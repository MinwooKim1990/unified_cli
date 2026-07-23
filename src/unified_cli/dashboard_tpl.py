"""Packaged static resources for the browser dashboard.

Only the three declared dashboard assets can be read.  Keeping this loader
small and allowlisted makes it safe for HTTP routes to call without exposing
arbitrary package paths.
"""

from __future__ import annotations

from importlib import resources
from typing import Tuple


_ASSETS = {
    "dashboard.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def load_dashboard_asset(name: str) -> Tuple[str, str]:
    """Return one approved dashboard asset and its fixed MIME type.

    ``name`` must be an exact basename from ``_ASSETS``.  In particular, this
    deliberately rejects path separators, traversal, and future undeclared
    package files rather than normalizing an arbitrary path.
    """
    if name not in _ASSETS:
        raise ValueError("unknown dashboard asset")
    content = resources.files("unified_cli").joinpath("web", name).read_text(
        encoding="utf-8"
    )
    return content, _ASSETS[name]


# Retained for server imports and consumers which historically imported the
# inline template directly.  It is decoded from the packaged HTML resource.
DASHBOARD_HTML, _DASHBOARD_MIME = load_dashboard_asset("dashboard.html")
