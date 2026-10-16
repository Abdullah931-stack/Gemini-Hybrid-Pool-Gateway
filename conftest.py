"""Global pytest configuration and Windows C-extension compatibility hook."""

from __future__ import annotations

from importlib.machinery import ExtensionFileLoader
from importlib.util import spec_from_file_location
import os
from pathlib import Path
import sys
from typing import Any

# Ensure workspace root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Install win_amd64.pyd finder for MSYS2 MinGW Python if needed
if sys.platform == "win32":
    class _WinAmd64PydFinder:
        @classmethod
        def find_spec(cls, fullname: str, path: Any = None, target: Any = None) -> Any:
            search_paths = path if path is not None else sys.path
            name = fullname.rpartition(".")[2]
            for p in search_paths:
                candidate = os.path.join(p, f"{name}.cp311-win_amd64.pyd")
                if os.path.isfile(candidate):
                    return spec_from_file_location(
                        fullname, candidate, loader=ExtensionFileLoader(fullname, candidate)
                    )
            return None

    if not any(getattr(f, "__name__", "") == "_WinAmd64PydFinder" for f in sys.meta_path):
        sys.meta_path.insert(0, _WinAmd64PydFinder)

# Default safe mock OAuth credentials for unit and integration testing
os.environ.setdefault("GEMINI_OAUTH_CLIENT_ID", "mock-test-client-id.apps.googleusercontent.com")
os.environ.setdefault("GEMINI_OAUTH_CLIENT_SECRET", "mock-test-client-secret")

