"""
Raw F1 Live Timing data fetcher.

Downloads .jsonStream files directly from livetiming.formula1.com
and stores them locally for replay.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import aiohttp

from app.config import REPLAY_DEBUG
from app.processing.database import transient_db_path
from app.processing.preprocessor import SessionPreProcessor

logger = logging.getLogger(__name__)


def is_jsonl_complete(live_file: Path) -> bool:
    """True if the recording captured a whole session start-to-end.

    F1 closes every session with a SessionStatus ``{"Status": "Ends"}``
    message; a capture cut short (crash, interrupted download) lacks it.
    The marker sits near the tail, so only the last chunk is read.
    """
    try:
        size = live_file.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    try:
        with live_file.open("rb") as fh:
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return False
    return '"Status": "Ends"' in tail

# Base URL for F1 live timing static data
LIVETIMING_BASE_URL = "https://livetiming.formula1.com/static"

# Common topics available in all session types
_COMMON_TOPICS = [
    "Heartbeat",
    "CarData.z",
    "Position.z",
    "ExtrapolatedClock",
    "TopThree",
    "TimingStats",
    "TimingAppData",
    "WeatherData",
    "TrackStatus",
    "DriverList",
    "RaceControlMessages",
    "SessionInfo",
    "SessionData",
    "TimingData",
    "TimingDataF1",
    "TeamRadio",
    "PitLaneTimeCollection",
    "PitStopSeries",
    "PitStop",
    # New topics (discovered 2026)
    "AudioStreams",
    "ContentStreams",
    "CurrentTyres",
    "TyreStintSeries",
    "LapSeries",
    "WeatherDataSeries",
    "DriverTracker",
    "ArchiveStatus",
    "SessionStatus",
    "TlaRcm",
]

# Topics to fetch for race sessions
RACE_TOPICS = _COMMON_TOPICS + [
    "LapCount",
    "ChampionshipPrediction",
    # Race-only topics
    "DriverRaceInfo",
    "OvertakeSeries",
]

# Topics for non-race sessions (practice, qualifying)
NON_RACE_TOPICS = _COMMON_TOPICS


@dataclass
class Meeting:
    """F1 Meeting (Grand Prix weekend)."""
    key: int
    name: str
    official_name: str
    location: str
    country: str
    circuit: str
    number: int = 0  # Round number (1-based, 0 for testing)
    sessions: list["Session"] = field(default_factory=list)


@dataclass
class Session:
    """F1 Session within a meeting."""
    key: int
    name: str
    type: str
    path: Optional[str]
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    gmt_offset: Optional[str]


@dataclass
class RawTimingMessage:
    """A single raw timing message."""
    topic: str
    timestamp: datetime
    offset: timedelta  # Offset from session start
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "Type": self.topic,
            "DateTime": self.timestamp.isoformat(),
            "Offset": str(self.offset),
            "Json": self.data,
        }


class LiveTimingFetcher:
    """Fetches raw timing data from F1's livetiming service."""

    def __init__(self, cache_dir: str = "data/livetiming_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": "F1-Timing-App/1.0"},
            )
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_meetings(self, year: int) -> list[Meeting]:
        """Get all meetings (Grand Prix weekends) for a year."""
        url = f"{LIVETIMING_BASE_URL}/{year}/Index.json"
        logger.info(f"Fetching meetings index: {url}")

        session = await self._get_session()
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                # Handle potential BOM in response
                text = await response.text()
                # Remove BOM if present
                if text.startswith('\ufeff'):
                    text = text[1:]
                data = json.loads(text)
        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch meetings: {e}")
            raise

        meetings = []
        round_number = 0
        raw_meetings = data.get("Meetings", [])
        logger.info(f"API returned {len(raw_meetings)} meetings for {year}")

        for m in raw_meetings:
            try:
                sessions = []
                for s in m.get("Sessions", []):
                    start_date = None
                    end_date = None
                    if s.get("StartDate"):
                        try:
                            start_date = datetime.fromisoformat(s["StartDate"].replace("Z", "+00:00"))
                        except ValueError:
                            try:
                                start_date = datetime.fromisoformat(s["StartDate"])
                            except ValueError:
                                logger.warning(f"Could not parse StartDate: {s.get('StartDate')}")
                    if s.get("EndDate"):
                        try:
                            end_date = datetime.fromisoformat(s["EndDate"].replace("Z", "+00:00"))
                        except ValueError:
                            try:
                                end_date = datetime.fromisoformat(s["EndDate"])
                            except ValueError:
                                logger.warning(f"Could not parse EndDate: {s.get('EndDate')}")

                    sessions.append(Session(
                        key=s.get("Key", 0),
                        name=s.get("Name", ""),
                        type=s.get("Type", ""),
                        path=s.get("Path"),
                        start_date=start_date,
                        end_date=end_date,
                        gmt_offset=s.get("GmtOffset"),
                    ))

                country = m.get("Country", {})
                circuit = m.get("Circuit", {})
                meeting_name = m.get("Name", "")

                # Assign round number (testing events get 0)
                is_testing = "test" in meeting_name.lower()
                if not is_testing:
                    round_number += 1
                event_number = 0 if is_testing else round_number

                meetings.append(Meeting(
                    key=m.get("Key", 0),
                    name=meeting_name,
                    official_name=m.get("OfficialName", ""),
                    location=m.get("Location", ""),
                    country=country.get("Name", "") if isinstance(country, dict) else str(country),
                    circuit=circuit.get("ShortName", "") if isinstance(circuit, dict) else str(circuit),
                    number=event_number,
                    sessions=sessions,
                ))
            except Exception as e:
                logger.error(f"Failed to parse meeting: {m.get('Name', 'unknown')} - {e}")

        logger.info(f"Found {len(meetings)} meetings for {year}")

        # Migrate old-format cache directories (NN_Location -> MeetingKey_Location)
        self._migrate_cache_dirs(year, meetings)

        return meetings

    async def get_session_by_name(
        self, year: int, meeting_name: str, session_type: str,
        meeting_key: int | None = None,
    ) -> Optional[tuple[Meeting, Session]]:
        """Find a session by meeting name and session type.

        If meeting_key is provided, matches by unique F1 API key directly
        (required to disambiguate testing events with the same location).
        """
        meetings = await self.get_meetings(year)

        # Normalize session type
        session_type_map = {
            "FP1": "Practice 1",
            "FP2": "Practice 2",
            "FP3": "Practice 3",
            "Q": "Qualifying",
            "SQ": "Sprint Qualifying",
            "S": "Sprint",
            "R": "Race",
            "Practice 1": "Practice 1",
            "Practice 2": "Practice 2",
            "Practice 3": "Practice 3",
            "Qualifying": "Qualifying",
            "Sprint Qualifying": "Sprint Qualifying",
            "Sprint": "Sprint",
            "Race": "Race",
        }
        target_session = session_type_map.get(session_type, session_type)

        for meeting in meetings:
            if meeting_key is not None:
                if meeting.key != meeting_key:
                    continue
            else:
                # Match by location or name (flexible matching)
                if not (meeting_name.lower() in meeting.name.lower() or
                        meeting_name.lower() in meeting.location.lower()):
                    continue

            # First try exact name match
            for session in meeting.sessions:
                if session.name == target_session:
                    return meeting, session
            # Fallback to type match only if no name match found
            for session in meeting.sessions:
                if session.type == session_type and session.name not in ["Sprint"]:
                    return meeting, session

        return None

    def _get_session_cache_dir(self, year: int, meeting: Meeting, session: Session) -> Path:
        """Get the cache directory for a session.

        Structure: year/MeetingKey_Location/SessionKey_Session_Name/
        Example: 2025/1254_Melbourne/9876_Race/

        Uses session.key as prefix for unique identification. The session key
        alone is sufficient for matching, regardless of what the folder is called.
        """
        # Clean location (replace spaces with underscores)
        location = meeting.location.replace(" ", "_")
        # Clean session name with session key prefix
        session_name = session.name.replace(" ", "_")
        session_folder = f"{session.key}_{session_name}" if session.key else session_name

        new_dir = self.cache_dir / str(year) / f"{meeting.key}_{location}"

        # Migrate from old format (NN_Location) if it exists
        if not new_dir.exists():
            old_dir = self.cache_dir / str(year) / f"{meeting.number:02d}_{location}"
            if old_dir.exists():
                old_dir.rename(new_dir)
                logger.info(f"Migrated cache directory: {old_dir.name} -> {new_dir.name}")

        target = new_dir / session_folder

        # Migrate old session folders without key prefix
        if not target.exists():
            old_session = new_dir / session_name
            if old_session.exists():
                old_session.rename(target)
                logger.info(f"Migrated session directory: {old_session.name} -> {target.name}")

        return target

    def _migrate_cache_dirs(self, year: int, meetings: list[Meeting]) -> None:
        """Migrate old-format cache directories (NN_Location) to new (MeetingKey_Location).

        Uses subscribe.json to verify which meeting the data belongs to,
        important for testing events that shared the same old directory name.
        """
        year_dir = self.cache_dir / str(year)
        if not year_dir.exists():
            return

        for meeting in meetings:
            location = meeting.location.replace(" ", "_")
            new_dir = year_dir / f"{meeting.key}_{location}"
            if new_dir.exists():
                continue
            old_dir = year_dir / f"{meeting.number:02d}_{location}"
            if not old_dir.exists():
                continue

            # For dirs that could collide (e.g., testing events sharing 00_Bahrain),
            # verify the data actually belongs to this meeting via subscribe.json
            actual_key = self._read_meeting_key_from_dir(old_dir)
            if actual_key is not None and actual_key != meeting.key:
                continue

            old_dir.rename(new_dir)
            logger.info(f"Migrated cache directory: {old_dir.name} -> {new_dir.name}")

    @staticmethod
    def _read_meeting_key_from_dir(event_dir: Path) -> int | None:
        """Read the meeting key from subscribe.json in any session subdirectory."""
        for session_dir in event_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sub = session_dir / "subscribe.json"
            if sub.exists():
                try:
                    with open(sub) as f:
                        data = json.load(f)
                    return data.get("SessionInfo", {}).get("Meeting", {}).get("Key")
                except (json.JSONDecodeError, KeyError):
                    pass
        return None

    @staticmethod
    def _dir_downloaded(session_dir: Path) -> bool:
        """True if the SESSION DATA is downloaded (live.jsonl present).

        Whether that recording captured the whole session is reported
        separately via the per-session `has_jsonl` flag (see
        `is_jsonl_complete`) which drives the recording-status icon in
        the UI; a partial capture still appears here, just with a muted
        icon.
        """
        live = session_dir / "live.jsonl"
        return live.exists() and live.stat().st_size > 0

    def is_session_cached(self, year: int, meeting: Meeting, session: Session) -> bool:
        """Check if a session is already cached AND ready to use.

        "Ready" means live.jsonl plus a complete session.db (testing
        sessions: live.jsonl alone — see `_dir_downloaded`). Checks both
        new format (SessionKey_Name) and old format (Name only),
        including matching by session key prefix alone (handles naming
        mismatches between live capture and download).
        """
        cache_dir = self._get_session_cache_dir(year, meeting, session)
        if self._dir_downloaded(cache_dir):
            return True

        # Check if any sibling folder starts with the session key
        if session.key:
            event_dir = cache_dir.parent
            if event_dir.exists():
                prefix = f"{session.key}_"
                for d in event_dir.iterdir():
                    if d.is_dir() and d.name.startswith(prefix):
                        if self._dir_downloaded(d):
                            return True

        return False

    def get_cached_sessions(self) -> list[dict[str, Any]]:
        """Get list of all cached sessions.

        Scans hierarchical structure: year/NN_Event_Name/Session_Name/
        Also supports legacy flat structure for backwards compatibility.
        """
        sessions = []

        for year_dir in self.cache_dir.iterdir():
            if not year_dir.is_dir():
                continue

            # Check if this is a year directory (4 digits) or legacy flat structure
            if year_dir.name.isdigit() and len(year_dir.name) == 4:
                # New hierarchical structure: year/event/session
                year = year_dir.name
                for event_dir in year_dir.iterdir():
                    if not event_dir.is_dir():
                        continue

                    # Parse event directory name: NN_Location
                    event_parts = event_dir.name.split("_", 1)
                    event_number = event_parts[0] if len(event_parts) > 1 else "00"
                    location = event_parts[1].replace("_", " ") if len(event_parts) > 1 else event_dir.name

                    for session_dir in event_dir.iterdir():
                        if not session_dir.is_dir():
                            continue

                        live_file = session_dir / "live.jsonl"
                        # Only list sessions that are fully downloaded and
                        # ready (testing sessions: live.jsonl alone).
                        if not self._dir_downloaded(session_dir):
                            continue

                        session_name = session_dir.name.replace("_", " ")
                        # Create cache key: year_eventNum_location_session (includes event number for uniqueness)
                        cache_key = f"{year}_{event_number}_{location.replace(' ', '_')}_{session_dir.name}"

                        # Extract session key if folder has key prefix (e.g., "11235_Practice_1")
                        session_key = None
                        dir_parts = session_dir.name.split("_", 1)
                        if dir_parts[0].isdigit() and len(dir_parts) > 1:
                            session_key = dir_parts[0]

                        info = self._build_session_info(
                            session_dir, live_file, cache_key,
                            year, location, session_name
                        )
                        if session_key:
                            info["session_key"] = session_key
                        sessions.append(info)
            else:
                # Legacy flat structure: year_location_session
                live_file = year_dir / "live.jsonl"
                if self._dir_downloaded(year_dir):
                    info = self._build_session_info_legacy(year_dir, live_file)
                    sessions.append(info)

        return sorted(sessions, key=lambda x: x.get("modified", ""), reverse=True)

    def _build_session_info(
        self, session_dir: Path, live_file: Path, cache_key: str,
        year: str, location: str, session_name: str
    ) -> dict[str, Any]:
        """Build session info dict for a cached session."""
        subscribe_file = session_dir / "subscribe.json"

        info = {
            "name": cache_key,
            "path": str(session_dir),
            "year": year,
            "meeting": "",
            "location": location,
            "session": session_name,
            "session_type": "",
        }

        # Try to read additional info from subscribe.json
        if subscribe_file.exists():
            try:
                with open(subscribe_file, "r") as f:
                    subscribe_data = json.load(f)
                session_info = subscribe_data.get("SessionInfo", {})
                meeting_info = session_info.get("Meeting", {})
                info["meeting"] = meeting_info.get("Name", "")
                info["session_type"] = session_info.get("Type", "")
            except (json.JSONDecodeError, KeyError):
                pass

        # Get file stats
        stat = live_file.stat()
        info["size_mb"] = round(stat.st_size / (1024 * 1024), 2)
        info["modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()

        # Per-file presence flags so the UI can show recording / audio
        # status icons without hitting a second endpoint.
        info["has_jsonl"] = live_file.exists() and is_jsonl_complete(live_file)
        info["has_audio"] = any(
            (session_dir / f).exists() and (session_dir / f).stat().st_size > 0
            for f in ("commentary.aac", "commentary.001.aac")
        )

        return info

    def _build_session_info_legacy(self, session_dir: Path, live_file: Path) -> dict[str, Any]:
        """Build session info dict for legacy flat cache structure."""
        subscribe_file = session_dir / "subscribe.json"

        info = {"name": session_dir.name, "path": str(session_dir)}

        # Try to read session info
        if subscribe_file.exists():
            try:
                with open(subscribe_file, "r") as f:
                    subscribe_data = json.load(f)
                session_info = subscribe_data.get("SessionInfo", {})
                meeting_info = session_info.get("Meeting", {})
                info["year"] = session_dir.name.split("_")[0]
                info["meeting"] = meeting_info.get("Name", "")
                info["location"] = meeting_info.get("Location", "")
                info["session"] = session_info.get("Name", "")
                info["session_type"] = session_info.get("Type", "")
            except (json.JSONDecodeError, KeyError):
                pass

        # Get file stats
        stat = live_file.stat()
        info["size_mb"] = round(stat.st_size / (1024 * 1024), 2)
        info["modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()

        return info

    def find_cached_session_path(self, cache_key: str) -> Optional[Path]:
        """Find the actual path of a cached session by its cache key.

        The cache key format is: year_location_session (e.g., 2025_Melbourne_Race)
        This handles both new hierarchical and legacy flat structures.
        """
        # First, check all cached sessions and match by name/key
        for session in self.get_cached_sessions():
            if session["name"] == cache_key:
                return Path(session["path"])

        # Fallback: try legacy flat path
        legacy_path = self.cache_dir / cache_key
        if legacy_path.exists():
            return legacy_path

        return None

    async def fetch_session(
        self,
        year: int,
        meeting: Meeting,
        session: Session,
        force: bool = False,
        progress_callback: Optional[callable] = None,
    ) -> Path:
        """
        Fetch all raw timing data for a session.

        Args:
            year: Season year
            meeting: Meeting object
            session: Session object
            force: If True, re-download even if cached
            progress_callback: Optional callback(topic, status) for progress updates

        Returns:
            Path to the session cache directory
        """
        if not session.path:
            raise ValueError(f"Session {session.name} has no path - may not be completed yet")

        cache_dir = self._get_session_cache_dir(year, meeting, session)
        live_file = cache_dir / "live.jsonl"
        subscribe_file = cache_dir / "subscribe.json"

        if not force and live_file.exists():
            logger.info(f"Session already cached: {cache_dir}")
            return cache_dir

        logger.info(f"Fetching session: {year} {meeting.location} {session.name}")
        cache_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"{LIVETIMING_BASE_URL}/{session.path}"

        # Fetch SessionInfo and ExtrapolatedClock to calculate session start time.
        # ExtrapolatedClock has a Utc field (reliable) and always starts within
        # a few seconds of the actual session data. Heartbeat can start 30-55 min
        # early, causing all non-.z topics to get wrong timestamps.
        if progress_callback:
            progress_callback("SessionInfo", "fetching")

        session_info_json = await self._fetch_json_file(prefix, "SessionInfo")
        ec_data = await self._fetch_topic_data(prefix, "ExtrapolatedClock")

        session_start = None

        if ec_data:
            first_ec = ec_data[0]
            ec_utc = first_ec["data"].get("Utc")
            if ec_utc:
                try:
                    utc_time = datetime.fromisoformat(ec_utc.replace("Z", "+00:00"))
                except ValueError:
                    utc_time = datetime.fromisoformat(ec_utc)
                session_start = utc_time - first_ec["offset"]
                logger.info(f"Session start from ExtrapolatedClock: {session_start} "
                            f"(Utc={ec_utc}, offset={first_ec['offset']})")

        if not session_start:
            # Fallback: try Heartbeat
            heartbeat_data = await self._fetch_topic_data(prefix, "Heartbeat")
            if heartbeat_data:
                first_hb = heartbeat_data[0]
                hb_utc = first_hb["data"].get("Utc")
                if hb_utc:
                    try:
                        utc_time = datetime.fromisoformat(hb_utc.replace("Z", "+00:00"))
                    except ValueError:
                        utc_time = datetime.fromisoformat(hb_utc)
                    session_start = utc_time - first_hb["offset"]
                    logger.warning(f"Session start from Heartbeat (fallback): {session_start}")

        if not session_start:
            if session.start_date:
                session_start = session.start_date
                logger.warning(f"Using scheduled session start time: {session_start}")
            else:
                session_start = datetime.utcnow()
                logger.warning("Could not determine session start time, using current time")

        # Determine which topics to fetch — start with our known list,
        # then merge any new topics discovered from the session's Index.json
        topics = list(RACE_TOPICS if session.type == "Race" else NON_RACE_TOPICS)
        index_json = await self._fetch_json_file(prefix, "Index")
        if index_json and "Feeds" in index_json:
            known = set(topics)
            for feed_name in index_json["Feeds"]:
                if feed_name not in known:
                    topics.append(feed_name)
                    logger.info(f"Discovered new topic from Index.json: {feed_name}")

        # Fetch all topics concurrently
        all_messages = []

        async def fetch_topic(topic: str):
            if progress_callback:
                progress_callback(topic, "fetching")
            try:
                messages = await self._fetch_topic_data(prefix, topic, session_start)
                if progress_callback:
                    progress_callback(topic, f"done ({len(messages)} messages)")
                return messages
            except Exception as e:
                logger.warning(f"Failed to fetch {topic}: {e}")
                if progress_callback:
                    progress_callback(topic, f"failed: {e}")
                return []

        # Fetch topics with some concurrency limit
        semaphore = asyncio.Semaphore(5)

        async def fetch_with_semaphore(topic):
            async with semaphore:
                return await fetch_topic(topic)

        results = await asyncio.gather(*[fetch_with_semaphore(t) for t in topics])

        for messages in results:
            all_messages.extend(messages)

        # Sort by timestamp
        all_messages.sort(key=lambda m: m["timestamp"])

        logger.info(f"Fetched {len(all_messages)} total messages")

        # Write live.jsonl
        with open(live_file, "w", encoding="utf-8") as f:
            for msg in all_messages:
                line = {
                    "Type": msg["topic"],
                    "Json": msg["data"],
                    "DateTime": msg["timestamp"].isoformat(),
                }
                f.write(json.dumps(line) + "\n")

        # Write subscribe.json (initial state)
        subscribe_data = {}
        if session_info_json:
            subscribe_data["SessionInfo"] = session_info_json
        if ec_data:
            subscribe_data["ExtrapolatedClock"] = ec_data[0]["data"]

        with open(subscribe_file, "w", encoding="utf-8") as f:
            json.dump(subscribe_data, f, indent=2)

        logger.info(f"Session data saved to {cache_dir}")

        # Build and process the session even when nobody watches it, so pace
        # analysis and the (later-stage) session summary can run offline and
        # be persisted for reading afterwards. The processed DB itself is
        # transient: it is built in ./tmp, analysis results are persisted to
        # data/analysis/, and the DB is then deleted (kept in DEBUG mode).
        if progress_callback:
            progress_callback("Processing", "building session database")
        pre = SessionPreProcessor(cache_dir, "")
        try:
            # force=True so a re-download rebuilds from the new live.jsonl
            # rather than early-returning on a prior 'complete' status.
            await pre.run(
                force=True,
                on_progress=(
                    (lambda pct: progress_callback("Processing", f"{pct:.0f}%"))
                    if progress_callback else None
                )
            )
            # TODO(analysis, item 4): run the analysis pipeline here and
            # persist results to data/analysis/ before the DB is deleted.
        finally:
            pre.close()
        if not REPLAY_DEBUG:
            db_path = transient_db_path(cache_dir)
            for suffix in ("", "-wal", "-shm"):
                db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        logger.info(f"Session processed for {cache_dir}")

        return cache_dir

    async def _fetch_topic_data(
        self,
        url_prefix: str,
        topic: str,
        session_start: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch data for a single topic.

        Returns list of dicts with keys: topic, timestamp, offset, data
        """
        url = f"{url_prefix}{topic}.jsonStream"
        logger.debug(f"Fetching {topic} from {url}")

        session = await self._get_session()
        try:
            async with session.get(url) as response:
                if response.status == 404:
                    logger.debug(f"Topic {topic} not found (404)")
                    return []
                response.raise_for_status()
                raw_data = await response.text()
        except aiohttp.ClientError as e:
            logger.warning(f"Failed to fetch {topic}: {e}")
            return []

        # Handle BOM if present
        if raw_data.startswith('\ufeff'):
            raw_data = raw_data[1:]

        messages = []
        for line in raw_data.strip().split("\n"):
            if not line.strip():
                continue

            try:
                # Format: "HH:MM:SS.mmm{json}"
                # First 12 characters are the time offset
                offset_str = line[:12]
                json_str = line[12:]

                # Parse offset (time from session start)
                parts = offset_str.split(":")
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds_parts = parts[2].split(".")
                seconds = int(seconds_parts[0])
                milliseconds = int(seconds_parts[1]) if len(seconds_parts) > 1 else 0

                offset = timedelta(
                    hours=hours,
                    minutes=minutes,
                    seconds=seconds,
                    milliseconds=milliseconds,
                )

                # Calculate absolute timestamp
                if session_start:
                    timestamp = session_start + offset
                else:
                    timestamp = datetime.utcnow()  # Fallback

                # Parse JSON data
                data = json.loads(json_str)

                messages.append({
                    "topic": topic,
                    "timestamp": timestamp,
                    "offset": offset,
                    "data": data,
                })

            except (ValueError, json.JSONDecodeError) as e:
                logger.warning(f"Failed to parse line in {topic}: {e}")
                continue

        logger.debug(f"Parsed {len(messages)} messages from {topic}")
        return messages

    async def _fetch_json_file(
        self,
        url_prefix: str,
        name: str,
    ) -> Optional[dict[str, Any]]:
        """
        Fetch a .json file (not .jsonStream).

        Some files like SessionInfo are plain JSON, not the timestamped stream format.

        Returns the parsed JSON object, or None if not found.
        """
        url = f"{url_prefix}{name}.json"
        logger.debug(f"Fetching JSON file {name} from {url}")

        session = await self._get_session()
        try:
            async with session.get(url) as response:
                if response.status == 404:
                    logger.debug(f"JSON file {name} not found (404)")
                    return None
                response.raise_for_status()
                text = await response.text()
                # Handle BOM if present
                if text.startswith('\ufeff'):
                    text = text[1:]
                return json.loads(text)
        except aiohttp.ClientError as e:
            logger.warning(f"Failed to fetch JSON file {name}: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON file {name}: {e}")
            return None

    async def fetch_session_by_name(
        self,
        year: int,
        meeting_name: str,
        session_type: str,
        force: bool = False,
        progress_callback: Optional[callable] = None,
        meeting_key: int | None = None,
    ) -> Optional[Path]:
        """
        Convenience method to fetch a session by meeting name and session type.

        Args:
            year: Season year
            meeting_name: Meeting name or location (e.g., "Monaco", "Australian Grand Prix")
            session_type: Session type (e.g., "R", "Race", "Q", "FP1")
            force: If True, re-download even if cached
            progress_callback: Optional callback for progress updates
            meeting_key: Optional F1 API meeting key for exact match

        Returns:
            Path to the session cache directory, or None if not found
        """
        result = await self.get_session_by_name(year, meeting_name, session_type, meeting_key)
        if not result:
            logger.error(f"Session not found: {year} {meeting_name} {session_type}")
            return None

        meeting, session = result
        return await self.fetch_session(year, meeting, session, force, progress_callback)


# Global instance
livetiming_fetcher = LiveTimingFetcher()
