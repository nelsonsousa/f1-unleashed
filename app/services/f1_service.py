"""FastF1 wrapper service for schedule and session data."""

import fastf1
import pandas as pd


def get_event_schedule(year: int, include_testing: bool = False) -> pd.DataFrame:
    """
    Get the F1 event schedule for a given year.

    Args:
        year: The season year
        include_testing: If True, include pre-season testing events

    Returns:
        DataFrame with event schedule
    """
    schedule = fastf1.get_event_schedule(year, include_testing=include_testing)
    return schedule


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
