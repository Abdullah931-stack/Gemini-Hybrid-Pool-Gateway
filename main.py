"""Main application entrypoint and CLI engine for Gemini-Hybrid-Pool-Gateway.

Provides:
1. Production FastAPI application factory (create_app) with Lifespan lifecycle management:
   - Registers OpenAI protocol router at /v1.
   - Registers Dashboard control-plane router at /api.
   - Mounts static asset directory at /static.
   - Serves dashboard UI at / and /dashboard via FileResponse.
   - Configures permissive CORS middleware.
   - Background AsyncIOScheduler for periodic quota sync (5m), cookie flushing (5m),
     and idle chat/temp file cleanup (10m).
2. Robust Click CLI engine:
   - serve: Start the uvicorn ASGI server with configurable host, port, and reload.
   - list-accounts: Pretty table of registered OAuth and Web accounts and quotas.
   - sync-quotas: Immediate quota synchronization pass across all accounts.
   - status / health: Quick telemetry overview of pool health and available models.
   - add-oauth: Interactive Google OAuth PKCE authorization wizard.
   - add-web: Interactive Web Cookie account registration from Firefox or manual cookies.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import AsyncIterator
from urllib.parse import urlparse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import click
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from api.dashboard_routes import callback_router, router as dashboard_router
from api.openai_routes import router as openai_router
from config.settings import (
    BASE_DIR,
    OAuthAccount,
    OAuthCredentials,
    WebAccount,
    load_accounts_into_memory,
    save_accounts_snapshot,
    state_registry,
)
from core.auth_oauth_cli import (
    OAuthCallbackServer,
    authorize_oauth_via_isolated_firefox,
    build_authorization_url,
    complete_oauth_login,
    extract_code_from_manual_input,
    generate_pkce_pair,
)
from core.auth_web import (
    auto_discover_firefox_cookies,
    extract_firefox_cookies,
    get_account_cookie_cache_path,
    get_saved_info_hygiene_notice,
    login_web_via_isolated_firefox,
    parse_cookie_header_or_dict,
    parse_manual_cookies,
    save_account_cookies_isolated,
    web_session_manager,
)
from core.hybrid_scheduler import (
    calculate_oauth_account_score,
    calculate_web_account_score,
)
from core.model_registry import (
    DEFAULT_OAUTH_MODELS,
    discover_oauth_models,
    unified_model_registry,
)
from core.prefix_differ import cleanup_orphaned_temp_files
from core.usage_monitor import sync_all_registered_accounts

logger = logging.getLogger(__name__)

STATIC_DIR: Path = BASE_DIR / "static"
INDEX_HTML_PATH: Path = STATIC_DIR / "index.html"
_SAFE_ACCOUNT_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def _is_interactive_terminal() -> bool:
    """Return True if running in an interactive user terminal, False if headless or piped."""
    try:
        return sys.stdin.isatty() and os.getenv("DEBIAN_FRONTEND") != "noninteractive"
    except Exception:
        return False


# ============================================================================
# 1. Lifespan & Application Factory
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup initialization and graceful shutdown cleanup."""
    logger.info("Initializing Gemini Hybrid Pool Gateway...")

    load_accounts_into_memory()
    cleanup_orphaned_temp_files()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sync_all_registered_accounts,
        "interval",
        minutes=5,
        args=[state_registry],
        kwargs={"web_session_manager": web_session_manager},
        id="sync_quotas",
        replace_existing=True,
    )
    scheduler.add_job(
        web_session_manager.flush_all_cookies,
        "interval",
        minutes=5,
        id="flush_cookies",
        replace_existing=True,
    )
    scheduler.add_job(
        web_session_manager.cleanup_expired_chats,
        "interval",
        minutes=10,
        kwargs={"max_idle_seconds": 3600.0},
        id="cleanup_chats",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Background tasks scheduler started successfully.")

    try:
        yield
    finally:
        logger.info("Initiating graceful shutdown sequence...")
        try:
            scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Error stopping background scheduler: %s", exc)

        try:
            await web_session_manager.flush_all_cookies()
        except Exception as exc:
            logger.warning("Error flushing cookies on shutdown: %s", exc)

        try:
            await web_session_manager.close_all()
        except Exception as exc:
            logger.warning("Error closing web session clients on shutdown: %s", exc)

        try:
            from config.settings import get_lmdb_store
            get_lmdb_store().close()
        except Exception as exc:
            logger.warning("Error closing LMDB store on shutdown: %s", exc)

        try:
            await save_accounts_snapshot()
        except Exception as exc:
            logger.warning("Error persisting accounts snapshot on shutdown: %s", exc)

        logger.info("Shutdown sequence completed.")


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI production application."""
    application = FastAPI(
        title="Gemini Hybrid Pool Gateway",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(openai_router)
    application.include_router(callback_router)
    application.include_router(dashboard_router)

    if not STATIC_DIR.exists():
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    @application.get("/dashboard", include_in_schema=False)
    async def serve_dashboard() -> FileResponse:
        """Serve the dashboard web client."""
        if INDEX_HTML_PATH.exists():
            return FileResponse(str(INDEX_HTML_PATH))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dashboard UI not found. Ensure static/index.html exists.",
        )

    return application


app: FastAPI = create_app()


# ============================================================================
# 2. CLI Presentation Helpers
# ============================================================================


def _format_quotas_summary(account: OAuthAccount | WebAccount) -> str:
    """Format account quota buckets for tabular CLI display."""
    if isinstance(account, OAuthAccount):
        model_quotas = getattr(account.quota_state, "model_quotas", {})
        if not model_quotas:
            return "No quotas synced"
        items = [
            f"{model}: {int(round(quota.remaining_fraction * 100))}%"
            for model, quota in sorted(model_quotas.items())
        ]
        return ", ".join(items)

    pct_5h = round(account.usage.five_hour_pct * 100, 1)
    pct_weekly = round(account.usage.weekly_pct * 100, 1)
    return f"5h: {pct_5h}%, weekly: {pct_weekly}%"


def _format_account_score(account: OAuthAccount | WebAccount, now: float) -> str:
    """Compute and format priority score for CLI display."""
    try:
        if isinstance(account, OAuthAccount):
            model = account.discovered_models[0] if account.discovered_models else "gemini-3.8-flash"
            score = calculate_oauth_account_score(account, model, now=now)
        else:
            score = calculate_web_account_score(account, "gemini-4-argon", now=now)
        return f"{score:.4f}"
    except Exception:
        return "0.0000"


def ensure_accounts_loaded() -> None:
    """Ensure accounts are loaded into memory if not already present."""
    if not state_registry.oauth_accounts and not state_registry.web_accounts:
        load_accounts_into_memory()


def build_accounts_table() -> str:
    """Build a formatted table of all registered OAuth and Web accounts."""
    ensure_accounts_loaded()
    oauth_accs = state_registry.oauth_accounts
    web_accs = state_registry.web_accounts
    if not oauth_accs and not web_accs:
        return "No accounts registered. Use 'add-oauth' or 'add-web' to register accounts."

    now = time.time()
    rows: list[tuple[str, str, str, str, str]] = []
    for acc in oauth_accs:
        rows.append(
            (acc.id, "oauth", acc.status, _format_account_score(acc, now), _format_quotas_summary(acc))
        )
    for acc in web_accs:
        rows.append(
            (acc.id, "web", acc.status, _format_account_score(acc, now), _format_quotas_summary(acc))
        )

    headers = ("ID", "Channel", "Status", "Priority", "Quotas")
    col_widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    sep_line = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_line = "| " + " | ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths)) + " |"

    lines = [sep_line, header_line, sep_line]
    for row in rows:
        lines.append("| " + " | ".join(f"{col:<{w}}" for col, w in zip(row, col_widths)) + " |")
    lines.append(sep_line)
    return "\n".join(lines)


def build_status_summary() -> str:
    """Compile pool status, account breakdown, and available models."""
    ensure_accounts_loaded()
    oauth_accs = state_registry.oauth_accounts
    web_accs = state_registry.web_accounts
    all_accs = list(oauth_accs) + list(web_accs)
    total = len(all_accs)
    active = sum(1 for a in all_accs if a.status == "ACTIVE")
    cooldown = sum(1 for a in all_accs if a.status == "COOLDOWN")
    suspended = sum(1 for a in all_accs if a.status == "SUSPENDED")
    concurrency = sum(a.active_requests for a in all_accs)

    if total == 0:
        pool_health = "EMPTY"
    elif active == 0:
        pool_health = "EXHAUSTED"
    elif cooldown > 0 or suspended > 0:
        pool_health = "DEGRADED"
    else:
        pool_health = "HEALTHY"

    models = unified_model_registry.get_base_models()
    models_str = ", ".join(models) if models else "No models registered"

    return "\n".join(
        [
            "=" * 60,
            "              Gemini Hybrid Pool Gateway Status             ",
            "=" * 60,
            f"Pool Health:        {pool_health}",
            f"Total Accounts:     {total}",
            f"  - Active:         {active}",
            f"  - Cooldown:       {cooldown}",
            f"  - Suspended:      {suspended}",
            "Channel Breakdown:",
            f"  - OAuth (CLI):    {len(oauth_accs)}",
            f"  - Web (Cookies):  {len(web_accs)}",
            f"Active Concurrency: {concurrency}",
            f"Available Models ({len(models)}):",
            f"  {models_str}",
            "=" * 60,
        ]
    )


# ============================================================================
# 3. CLI Commands
# ============================================================================


@click.command(name="serve", help="Start the Gemini Hybrid Pool Gateway server.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host network interface to bind to.")
@click.option("--port", default=8000, show_default=True, type=int, help="Port to bind to.")
@click.option("--reload", is_flag=True, default=False, help="Enable automatic code reload.")
def serve(host: str, port: int, reload: bool) -> None:
    """Run the Uvicorn ASGI server."""
    click.echo(f"Starting Gemini Hybrid Pool Gateway on http://{host}:{port} (reload={reload})...")
    uvicorn.run("main:app", host=host, port=port, reload=reload)


@click.command(name="list-accounts", help="Print table of registered accounts and quotas.")
def list_accounts() -> None:
    """Print registered accounts table."""
    click.echo(build_accounts_table())


@click.command(name="sync-quotas", help="Execute immediate quota synchronization from CLI.")
def sync_quotas() -> None:
    """Run quota synchronization pass and print updated table."""
    ensure_accounts_loaded()
    click.echo("Syncing account quotas across pool...")
    try:
        asyncio.run(sync_all_registered_accounts(state_registry))
        asyncio.run(save_accounts_snapshot())
        click.secho("Quota synchronization completed successfully.", fg="green")
        click.echo(build_accounts_table())
    except Exception as exc:
        logger.error("Error during quota sync: %s", exc)
        raise click.ClickException(f"Quota sync failed: {exc}")


@click.command(name="status", help="Check pool health, accounts, and available models.")
def status_cmd() -> None:
    """Display pool status."""
    click.echo(build_status_summary())


@click.command(name="health", help="Alias for 'status'.")
@click.pass_context
def health_cmd(ctx: click.Context) -> None:
    """Display pool health (alias for status)."""
    ctx.invoke(status_cmd)


def _display_auth_url(auth_url: str) -> None:
    """Display authorization URL instructions for OAuth flow."""
    click.echo("\n--- Step 1: Authorize with Google ---")
    click.echo("Open the following URL in your browser to authorize Antigravity CLI:\n")
    click.echo(auth_url)


def _obtain_oauth_code(
    pkce: PKCEChallenge,
    redirect_uri: str,
    is_interactive: bool,
) -> tuple[str, str]:
    """Obtain OAuth code either via isolated Firefox or manual input fallback."""
    code: str | None = None
    if is_interactive:
        click.echo("Opening Google authorization page in isolated Firefox...")
        click.echo("Awaiting Google sign-in in isolated Firefox (press Ctrl+C to cancel)...")
        try:
            code, redirect_uri = asyncio.run(
                authorize_oauth_via_isolated_firefox(
                    pkce=pkce,
                    preferred_port=8000,
                    timeout=180.0,
                )
            )
        except KeyboardInterrupt:
            click.echo("\nCancelled browser callback wait. Switching to manual input.")
        except Exception as exc:
            logger.debug("Automatic callback listener not active or timed out: %s", exc)
            click.echo(f"\nNotice: Automatic callback listener not active ({exc}). Switching to manual input.")

    if not code:
        if not is_interactive:
            click.echo("\n--- Step 2: Complete Authentication ---")
        user_input = click.prompt("Paste the redirected URL or authorization code")
        cleaned_input = user_input.strip()
        if cleaned_input.startswith(("http://", "https://")):
            parsed_url = urlparse(cleaned_input)
            if parsed_url.netloc:
                redirect_uri = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        code = extract_code_from_manual_input(cleaned_input, expected_state=pkce.state)

    return code, redirect_uri


def _complete_and_register_oauth(
    account_id: str,
    label: str | None,
    mode: str,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> None:
    """Exchange code for credentials, discover models, and persist account."""
    click.echo("\nExchanging code for tokens and discovering cloud project...")
    try:
        credentials = asyncio.run(
            complete_oauth_login(
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=verifier,
            )
        )
        account = OAuthAccount(
            id=account_id,
            label=label or account_id,
            transport_mode=mode,
            credentials=credentials,
            status="ACTIVE",
        )
        try:
            discovered = asyncio.run(discover_oauth_models(account))
            if discovered:
                account.discovered_models = discovered
        except Exception as exc:
            logger.warning("OAuth model discovery notice for '%s': %s", account_id, exc)

        if not account.discovered_models:
            account.discovered_models = list(DEFAULT_OAUTH_MODELS)
        state_registry.upsert_oauth_account(account)
        asyncio.run(save_accounts_snapshot())

        click.secho(
            f"Successfully registered OAuth account '{account_id}' "
            f"(Project: {credentials.project_id or 'unknown'}, "
            f"Models: {len(account.discovered_models)})!",
            fg="green",
        )
    except Exception as exc:
        logger.error("OAuth registration failed: %s", exc)
        raise click.ClickException(f"OAuth registration failed: {exc}")


@click.command(name="add-oauth", help="Interactive wizard to add an OAuth CLI account.")
@click.option("--id", "account_id", default=None, help="Unique identifier for the OAuth account.")
@click.option("--label", default=None, help="Human-readable label for the OAuth account.")
@click.option(
    "--mode",
    type=click.Choice(["http_v1internal", "agy_subprocess"]),
    default="http_v1internal",
    help="OAuth transport mode.",
)
def add_oauth(
    account_id: str | None = None,
    label: str | None = None,
    mode: str = "http_v1internal",
) -> None:
    """Interactive OAuth account registration."""
    ensure_accounts_loaded()
    if not account_id:
        default_id = f"oauth-{secrets.token_hex(4)}" if not label else label.lower().replace(" ", "-")
        account_id = click.prompt("Enter Account ID", default=default_id)
    account_id = account_id.strip()

    if not _SAFE_ACCOUNT_ID_RE.match(account_id):
        raise click.BadParameter("Account ID must be alphanumeric and safe for file paths.")

    pkce = generate_pkce_pair()
    redirect_uri = "http://localhost:8000/oauth/callback"
    auth_url = build_authorization_url(redirect_uri=redirect_uri, pkce=pkce)
    _display_auth_url(auth_url)

    is_interactive = _is_interactive_terminal()
    code, redirect_uri = _obtain_oauth_code(pkce, redirect_uri, is_interactive)
    _complete_and_register_oauth(account_id, label, mode, code, redirect_uri, pkce.code_verifier)


def _discover_and_persist_web_account(account: WebAccount) -> None:
    """Discover models for new web account and persist snapshot."""
    try:
        from core.auth_web import default_web_session_manager
        from core.model_registry import discover_web_models
        from core.usage_monitor import sync_web_account_usage

        client = asyncio.run(default_web_session_manager.get_or_init_client(account))
        asyncio.run(sync_web_account_usage(account, client=client, persist=False))
        discovered = asyncio.run(discover_web_models(account, client))
        if discovered:
            account.discovered_models = discovered
    except Exception as exc:
        logger.warning("Web account model discovery notice: %s", exc)

    if not account.discovered_models:
        account.discovered_models = [
            "gemini-4-argon",
            "gemini-3.1-pro",
            "gemini-3.8-flash",
            "gemini-3.1-flash-lite",
        ]
    asyncio.run(save_accounts_snapshot())


def _prompt_for_web_cookies() -> WebCookies:
    """Prompt user for cookie input or browser launch."""
    click.echo("\nSelect cookie source:")
    click.echo("  1. Firefox profile directory or cookies.sqlite file")
    click.echo("  2. Manual cookie input (Cookie header, JSON, or __Secure-1PSID)")
    click.echo("  3. Launch isolated Firefox to sign in")
    choice = click.prompt("Choose option [1, 2, or 3]", default="2", type=click.Choice(["1", "2", "3"]))

    if choice == "1":
        firefox_path = click.prompt("Enter Firefox cookies.sqlite or profile path")
        return extract_firefox_cookies(firefox_path.strip())
    if choice == "3":
        return asyncio.run(login_web_via_isolated_firefox(timeout_seconds=180.0))

    cookie_input = click.prompt("Enter Cookie header, JSON, or __Secure-1PSID")
    cleaned_input = cookie_input.strip()
    if "__Secure-1PSID" in cleaned_input or ";" in cleaned_input or "{" in cleaned_input:
        return parse_cookie_header_or_dict(cleaned_input)
    psidts_input = click.prompt("Enter __Secure-1PSIDTS (optional)", default="", show_default=False)
    return parse_manual_cookies(cleaned_input, psidts_input)


@click.command(name="add-web", help="Interactive wizard to add a Web Cookie account.")
@click.option("--id", "account_id", default=None, help="Unique identifier for the Web account.")
@click.option("--label", default=None, help="Human-readable label for the Web account.")
@click.option("--firefox-profile", default=None, help="Firefox profile name or cookies.sqlite path to import.")
def add_web(
    account_id: str | None = None,
    label: str | None = None,
    firefox_profile: str | None = None,
) -> None:
    """Interactive Web account registration."""
    ensure_accounts_loaded()
    if not account_id:
        default_id = f"web-{secrets.token_hex(4)}" if not label else label.lower().replace(" ", "-")
        account_id = click.prompt("Enter Account ID", default=default_id)
    account_id = account_id.strip()

    if not _SAFE_ACCOUNT_ID_RE.match(account_id):
        raise click.BadParameter("Account ID must be alphanumeric and safe for file paths.")

    try:
        parsed_cookies: WebCookies | None = None
        is_interactive = _is_interactive_terminal()

        if firefox_profile and firefox_profile.strip().lower() not in ("default-release", "default"):
            parsed_cookies = extract_firefox_cookies(firefox_profile.strip())
        else:
            sqlite_path, detected = auto_discover_firefox_cookies()
            if detected and detected.secure_1psid:
                parsed_cookies = detected
                click.secho(
                    f"✓ Automatically detected Gemini cookies from Firefox ({sqlite_path or 'browser profile'})!",
                    fg="cyan",
                )
            elif is_interactive:
                click.echo("\nNo active Gemini session found in Firefox.")
                click.echo("Launching isolated Firefox to capture credentials automatically...")
                try:
                    parsed_cookies = asyncio.run(login_web_via_isolated_firefox(timeout_seconds=180.0))
                    click.secho("✓ Successfully captured cookies from isolated Firefox!", fg="cyan")
                except Exception as exc:
                    logger.debug("Isolated browser login failed: %s", exc)

        if not parsed_cookies:
            parsed_cookies = _prompt_for_web_cookies()

        cookie_file_path = get_account_cookie_cache_path(account_id)
        account = WebAccount(
            id=account_id,
            label=label or account_id,
            cookie_file=str(cookie_file_path),
            cookies=parsed_cookies,
            status="ACTIVE",
            discovered_models=[],
        )

        save_account_cookies_isolated(account, sync_snapshot=False)
        state_registry.upsert_web_account(account)
        _discover_and_persist_web_account(account)

        click.secho(f"Successfully registered Web account '{account_id}'!", fg="green")
        click.echo("\n--- Hygiene Notice ---")
        click.echo(get_saved_info_hygiene_notice())
    except Exception as exc:
        logger.error("Web account registration failed: %s", exc)
        raise click.ClickException(f"Web account registration failed: {exc}")


# ============================================================================
# 4. Main CLI Group
# ============================================================================


@click.group(invoke_without_command=True)
@click.option("--serve", "flag_serve", is_flag=True, help="Start the gateway server.")
@click.option("--port", "flag_port", type=int, default=None, help="Port to bind when running server.")
@click.option("--add-oauth", "flag_add_oauth", is_flag=True, help="Add an OAuth CLI account.")
@click.option("--add-web", "flag_add_web", is_flag=True, help="Add a Web Cookie account.")
@click.option("--id", "flag_id", type=str, default=None, help="Unique identifier for the account.")
@click.option("--label", "flag_label", type=str, default=None, help="Account label.")
@click.option(
    "--mode",
    "flag_mode",
    type=click.Choice(["http_v1internal", "agy_subprocess"]),
    default="http_v1internal",
    help="OAuth transport mode.",
)
@click.option("--firefox-profile", "flag_firefox_profile", type=str, default=None, help="Firefox profile name to import.")
@click.option("--list-accounts", "flag_list_accounts", is_flag=True, help="List registered accounts.")
@click.option("--sync-quotas", "flag_sync_quotas", is_flag=True, help="Sync account quotas immediately.")
@click.option("--status", "flag_status", is_flag=True, help="Check pool health and status.")
@click.pass_context
def cli(
    ctx: click.Context,
    flag_serve: bool,
    flag_port: int | None,
    flag_add_oauth: bool,
    flag_add_web: bool,
    flag_id: str | None,
    flag_label: str | None,
    flag_mode: str,
    flag_firefox_profile: str | None,
    flag_list_accounts: bool,
    flag_sync_quotas: bool,
    flag_status: bool,
) -> None:
    """Gemini Hybrid Pool Gateway CLI & Management Interface."""
    if flag_serve or (ctx.invoked_subcommand is None and not any([flag_list_accounts, flag_sync_quotas, flag_status, flag_add_oauth, flag_add_web])):
        kwargs = {}
        if flag_port is not None:
            kwargs["port"] = flag_port
        ctx.invoke(serve, **kwargs)
        return
    if flag_add_oauth:
        kwargs = {"mode": flag_mode}
        if flag_id is not None:
            kwargs["account_id"] = flag_id
        if flag_label is not None:
            kwargs["label"] = flag_label
        ctx.invoke(add_oauth, **kwargs)
        return
    if flag_add_web:
        kwargs = {}
        if flag_id is not None:
            kwargs["account_id"] = flag_id
        if flag_firefox_profile is not None:
            kwargs["firefox_profile"] = flag_firefox_profile
        if flag_label is not None:
            kwargs["label"] = flag_label
        ctx.invoke(add_web, **kwargs)
        return
    if flag_list_accounts:
        ctx.invoke(list_accounts)
        return
    if flag_sync_quotas:
        ctx.invoke(sync_quotas)
        return
    if flag_status:
        ctx.invoke(status_cmd)
        return


cli.add_command(serve)
cli.add_command(list_accounts)
cli.add_command(sync_quotas)
cli.add_command(status_cmd)
cli.add_command(health_cmd)
cli.add_command(add_oauth)
cli.add_command(add_web)


if __name__ == "__main__":
    cli()
