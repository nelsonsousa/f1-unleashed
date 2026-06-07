"""Manager for parsing and listing livetiming cache contents."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CachedSession:
    """A cached session with its data files."""

    year: int
    round_number: int
    location: str
    session_name: str
    live_jsonl_size_bytes: int = 0
    live_jsonl_size_mb: float = 0.0
    subscribe_json_size_bytes: int = 0
    cache_path: str = ""


@dataclass
class CachedEvent:
    """A cached event with its sessions."""

    year: int
    round_number: int
    location: str
    sessions: list[CachedSession] = field(default_factory=list)
    total_size_bytes: int = 0
    total_size_mb: float = 0.0


class CacheManager:
    """Manages reading and parsing the livetiming cache directory."""

    def __init__(self, cache_dir: str = "./data/livetiming_cache"):
        self.cache_dir = Path(cache_dir)

    def get_cached_sessions(self) -> list[CachedEvent]:
        """
        Parse the cache directory and return structured data about cached sessions.

        Returns a list of events, each containing their cached sessions.
        """
        if not self.cache_dir.exists():
            logger.warning(f"Cache directory does not exist: {self.cache_dir}")
            return []

        events = []

        # Iterate year directories
        for year_dir in sorted(self.cache_dir.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue

            year = int(year_dir.name)

            # Iterate event directories within year
            for event_dir in sorted(year_dir.iterdir()):
                if not event_dir.is_dir():
                    continue

                event = self._parse_event_directory(year, event_dir)
                if event and event.sessions:
                    events.append(event)

        return events

    def _parse_event_directory(self, year: int, event_dir: Path) -> Optional[CachedEvent]:
        """Parse an event directory (e.g., '23_Lusail')."""
        # Extract round number and location from directory name
        # Format: {round}_{location}
        match = re.match(r"(\d+)_(.+)", event_dir.name)
        if not match:
            logger.debug(f"Skipping non-event directory: {event_dir.name}")
            return None

        round_number = int(match.group(1))
        location = match.group(2).replace("_", " ")

        sessions = []
        total_size = 0

        # Iterate session directories within event
        for session_dir in sorted(event_dir.iterdir()):
            if not session_dir.is_dir():
                continue

            session = self._parse_session_directory(year, round_number, location, session_dir)
            if session:
                sessions.append(session)
                total_size += session.live_jsonl_size_bytes + session.subscribe_json_size_bytes

        return CachedEvent(
            year=year,
            round_number=round_number,
            location=location,
            sessions=sessions,
            total_size_bytes=total_size,
            total_size_mb=round(total_size / (1024 * 1024), 2),
        )

    def _parse_session_directory(
        self, year: int, round_number: int, location: str, session_dir: Path
    ) -> Optional[CachedSession]:
        """Parse a session directory (e.g., 'Practice_1', 'Qualifying', 'Race')."""
        session_name = session_dir.name.replace("_", " ")

        live_jsonl = session_dir / "live.jsonl"
        subscribe_json = session_dir / "subscribe.json"

        # Must have at least live.jsonl
        if not live_jsonl.exists():
            return None

        live_size = live_jsonl.stat().st_size
        subscribe_size = subscribe_json.stat().st_size if subscribe_json.exists() else 0

        return CachedSession(
            year=year,
            round_number=round_number,
            location=location,
            session_name=session_name,
            live_jsonl_size_bytes=live_size,
            live_jsonl_size_mb=round(live_size / (1024 * 1024), 2),
            subscribe_json_size_bytes=subscribe_size,
            cache_path=str(session_dir),
        )

    def get_cache_summary(self) -> dict:
        """Get a summary of the entire cache."""
        events = self.get_cached_sessions()

        total_sessions = sum(len(e.sessions) for e in events)
        total_size = sum(e.total_size_bytes for e in events)

        return {
            "cache_path": str(self.cache_dir),
            "total_events": len(events),
            "total_sessions": total_sessions,
            "total_bytes": total_size,
            "total_mb": round(total_size / (1024 * 1024), 2),
        }

    def is_session_cached(self, year: int, location: str, session_type: str) -> bool:
        """
        Check if a specific session is already cached.

        Args:
            year: The year (e.g., 2025)
            location: The location name (e.g., "Lusail", "Melbourne")
            session_type: The session type (e.g., "Practice 1", "Race")
        """
        events = self.get_cached_sessions()

        for event in events:
            if event.year != year:
                continue

            # Fuzzy match on location
            if location.lower() not in event.location.lower():
                continue

            for session in event.sessions:
                if session_type.lower() in session.session_name.lower():
                    return True

        return False

    def get_cached_session_keys(self) -> set[str]:
        """
        Get a set of all cached session keys for quick lookup.

        Keys are in format: "year_location_sessionname" (normalized)
        """
        keys = set()
        events = self.get_cached_sessions()

        for event in events:
            for session in event.sessions:
                key = f"{event.year}_{event.location}_{session.session_name}"
                keys.add(key)

        return keys

    def to_dict(self) -> dict:
        """Get all cached sessions as a dictionary for API response."""
        events = self.get_cached_sessions()

        return {
            "events": [
                {
                    "year": e.year,
                    "round_number": e.round_number,
                    "location": e.location,
                    "total_size_mb": e.total_size_mb,
                    "sessions": [
                        {
                            "session_name": s.session_name,
                            "size_mb": s.live_jsonl_size_mb,
                            "cache_path": s.cache_path,
                        }
                        for s in e.sessions
                    ],
                }
                for e in events
            ]
        }


# Global instance
cache_manager = CacheManager()
