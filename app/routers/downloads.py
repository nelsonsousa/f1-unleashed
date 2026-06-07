"""Cache management endpoints for livetiming data."""

import logging

from fastapi import APIRouter

from app.services.cache_manager import cache_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/cache/sessions")
async def list_cached_sessions():
    """
    List all cached F1 sessions from livetiming data.

    Returns structured data about what sessions are cached on the server.
    """
    return cache_manager.to_dict()


@router.get("/cache/summary")
async def get_cache_summary():
    """Get a summary of cache usage (session count, total size)."""
    return cache_manager.get_cache_summary()


@router.get("/cache/check")
async def check_session_cached(
    year: int,
    location: str,
    session_type: str,
):
    """Check if a specific session is already cached."""
    is_cached = cache_manager.is_session_cached(year, location, session_type)
    return {
        "year": year,
        "location": location,
        "session_type": session_type,
        "is_cached": is_cached,
    }
