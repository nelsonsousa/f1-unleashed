"""FastF1 wrapper service for schedule and session data."""

import logging
import time

import fastf1
import pandas as pd

logger = logging.getLogger(__name__)

# The season schedule barely changes within a season, but FastF1 fetches it
# over the network from a chain of backends on every call. On a cold start
# (empty on-disk cache) that first fetch is slow and occasionally fails on all
# backends, which surfaced as the home page flashing "No upcoming sessions"
# until a second (now warm) reload. We keep an in-process copy so the endpoint
# serves a warm schedule, retry the cold fetch through transient failures, and
# fall back to the last-known-good copy so a network blip never wipes it.
_SCHEDULE_TTL_S = 3600            # 1h — a countdown tolerates this staleness
_SCHEDULE_FETCH_RETRIES = 3       # attempts before giving up on a cold fetch
_SCHEDULE_RETRY_BACKOFF_S = 1.0   # base backoff, multiplied by the attempt index

# {(year, include_testing): (DataFrame, fetched_at_monotonic)}
_schedule_cache: dict[tuple[int, bool], tuple[pd.DataFrame, float]] = {}


def get_event_schedule(year: int, include_testing: bool = False) -> pd.DataFrame:
    """
    Get the F1 event schedule for a given year.

    Cached in-process for ``_SCHEDULE_TTL_S`` and retried on transient FastF1
    failures. If a refresh fails but a previous copy exists, the stale copy is
    returned rather than propagating the error.

    Args:
        year: The season year
        include_testing: If True, include pre-season testing events

    Returns:
        DataFrame with event schedule
    """
    key = (year, include_testing)
    cached = _schedule_cache.get(key)
    if cached is not None and (time.monotonic() - cached[1]) < _SCHEDULE_TTL_S:
        return cached[0]

    last_error: Exception | None = None
    last_empty: pd.DataFrame | None = None
    for attempt in range(1, _SCHEDULE_FETCH_RETRIES + 1):
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=include_testing)
        except Exception as e:
            last_error = e
            logger.warning(
                "Schedule fetch for %s (testing=%s) failed on attempt %d/%d: %s",
                year, include_testing, attempt, _SCHEDULE_FETCH_RETRIES, e,
            )
        else:
            # Only a non-empty schedule is trustworthy. FastF1 can return an
            # empty frame (backends soft-failing) without raising; caching that
            # would poison the whole TTL window, so treat empty as a failure —
            # retry, and never cache it.
            if schedule is not None and len(schedule) > 0:
                _schedule_cache[key] = (schedule, time.monotonic())
                return schedule
            last_empty = schedule
            logger.warning(
                "Schedule fetch for %s (testing=%s) returned empty on attempt %d/%d",
                year, include_testing, attempt, _SCHEDULE_FETCH_RETRIES,
            )
        if attempt < _SCHEDULE_FETCH_RETRIES:
            time.sleep(_SCHEDULE_RETRY_BACKOFF_S * attempt)

    # Exhausted retries: prefer a previously-cached good schedule over a stale
    # failure so a transient blip never blanks the home countdown.
    if cached is not None:
        logger.warning(
            "Schedule refresh for %s (testing=%s) failed; serving stale copy.",
            year, include_testing,
        )
        return cached[0]
    if last_empty is not None:
        return last_empty  # genuinely empty (no prior good copy) — let caller 404
    raise last_error


def get_session(year: int, race: str | int, session_type: str):
    """
    Get a specific F1 session.

    Args:
        year: The season year
        race: Race name or round number
        session_type: Session type (R, Q, FP1, etc.)

    Returns:
        FastF1 Session object
    """
    session = fastf1.get_session(year, race, session_type)
    session.load()
    return session
