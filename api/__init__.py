"""API package for Gemini-Hybrid-Pool-Gateway."""

from __future__ import annotations

__all__ = ["openai_router", "dashboard_router"]


def __getattr__(name: str):
    if name == "openai_router":
        from api.openai_routes import router
        return router
    if name == "dashboard_router":
        from api.dashboard_routes import router
        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
