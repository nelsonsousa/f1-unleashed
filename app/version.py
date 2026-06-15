"""Running app version + GitHub release-update check.

The version is read from the top-level ``VERSION`` file so the app and the
release tags share a single source of truth. ``check_latest_release`` compares
it against the latest GitHub release and caches the result (the frontend polls
``/api/v1/version`` for the "update available" indicator).
"""

import time
from pathlib import Path

import requests

GITHUB_REPO = "nelsonsousa/f1-unleashed"
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_CACHE_TTL_S = 3600  # re-check GitHub at most hourly
_cache: dict = {"checked_at": 0.0, "data": None}


def get_version() -> str:
    """The running app version, from the VERSION file (e.g. '1.2')."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _parse(v: str) -> tuple:
    """Loose version → comparable int tuple ('v1.2.0' → (1, 2, 0))."""
    out = []
    for part in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def check_latest_release(force: bool = False) -> dict:
    """Return {version, latest, update_available, release_url}.

    Cached for an hour; never raises (GitHub/network errors → latest=None,
    update_available=False).
    """
    now = time.time()
    if not force and _cache["data"] is not None \
            and now - _cache["checked_at"] < _CACHE_TTL_S:
        return _cache["data"]

    current = get_version()
    result = {
        "version": current,
        "latest": None,
        "update_available": False,
        "release_url": None,
    }
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=4,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            tag = (data.get("tag_name") or "").strip()
            if tag:
                result["latest"] = tag
                result["release_url"] = data.get("html_url")
                result["update_available"] = _parse(tag) > _parse(current)
    except requests.RequestException:
        pass

    _cache["data"] = result
    _cache["checked_at"] = now
    return result
