"""Session replay manager for streaming timing data in simulated real-time."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

import pandas as pd

from app.services.session_data import session_manager

logger = logging.getLogger(__name__)


class ReplayStatus(str, Enum):
    LOADING = "loading"      # Initial state, fetching from FastF1
    STREAMING = "streaming"  # Frames arriving, playback possible
    READY = "ready"          # All frames loaded
    PLAYING = "playing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class TimingEntry:
    """A single timing entry for a driver at a point in time."""

    position: int
    driver: str
    driver_number: str
    team: str
    team_color: str
    gap: str  # Gap to leader
    interval: str  # Interval to car ahead
    lap: int
    last_lap_time: Optional[str] = None
    is_pit: bool = False
    is_out: bool = False
    tyre_compound: Optional[str] = None  # SOFT, MEDIUM, HARD, INTERMEDIATE, WET
    tyre_age: int = 0  # Laps on current tyre


@dataclass
class WeatherData:
    """Weather conditions at a point in time."""
    air_temp: Optional[float] = None
    track_temp: Optional[float] = None
    wind_speed: Optional[float] = None
    wind_direction: Optional[int] = None
    rainfall: bool = False
    humidity: Optional[float] = None


@dataclass
class RaceControlMessage:
    """A race control message."""
    time: str  # Session time as string
    message: str
    category: str = ""
    flag: str = ""


@dataclass
class TimingFrame:
    """A frame of timing data at a specific session time."""

    session_time: timedelta
    session_time_str: str
    entries: list[TimingEntry]
    flag_status: str = "GREEN"
    message: Optional[str] = None
    weather: Optional[WeatherData] = None
    race_control_messages: list[RaceControlMessage] = field(default_factory=list)


@dataclass
class ReplaySession:
    """A replay session with timing data."""

    replay_id: str
    year: int
    race: str
    session_type: str
    status: ReplayStatus = ReplayStatus.LOADING
    speed: float = 1.0
    is_race: bool = False

    # Timing frames indexed by session time
    frames: list[TimingFrame] = field(default_factory=list)
    current_frame_index: int = 0

    # Virtual clock for accurate timing sync
    playback_start_real: Optional[datetime] = None  # Real wall-clock time when playback started
    playback_start_session: Optional[timedelta] = None  # Session time when playback started

    # Metadata
    event_name: str = ""
    session_name: str = ""
    total_laps: Optional[int] = None
    estimated_total_laps: Optional[int] = None  # For progress during streaming
    drivers: dict[str, dict] = field(default_factory=dict)

    # Control
    created_at: datetime = field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None

    def get_current_session_time(self) -> Optional[timedelta]:
        """Calculate current session time based on virtual clock."""
        if self.playback_start_real is None or self.playback_start_session is None:
            return None
        elapsed_real = (datetime.utcnow() - self.playback_start_real).total_seconds()
        elapsed_session = elapsed_real * self.speed
        return self.playback_start_session + timedelta(seconds=elapsed_session)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id,
            "year": self.year,
            "race": self.race,
            "session_type": self.session_type,
            "status": self.status.value,
            "speed": self.speed,
            "is_race": self.is_race,
            "event_name": self.event_name,
            "session_name": self.session_name,
            "total_laps": self.total_laps,
            "estimated_total_laps": self.estimated_total_laps,
            "total_frames": len(self.frames),
            "frames_loaded": len(self.frames),
            "current_frame": self.current_frame_index,
            "drivers": self.drivers,
        }


class ReplayManager:
    """Manages session replays with timing data streaming."""

    def __init__(self):
        self._replays: dict[str, ReplaySession] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._replay_tasks: dict[str, asyncio.Task] = {}

    async def start_replay(
        self,
        year: int,
        race: str,
        session_type: str,
        speed: float = 1.0,
    ) -> ReplaySession:
        """Start a new replay session."""
        replay_id = str(uuid.uuid4())[:8]

        is_race = session_type in ("R", "Race", "S", "Sprint")

        replay = ReplaySession(
            replay_id=replay_id,
            year=year,
            race=race,
            session_type=session_type,
            speed=speed,
            is_race=is_race,
        )

        async with self._lock:
            self._replays[replay_id] = replay
            self._subscribers[replay_id] = []

        # Load data in background
        asyncio.create_task(self._load_replay_data(replay))

        logger.info(f"Created replay {replay_id} for {year} {race} {session_type}")
        return replay

    async def _load_replay_data(self, replay: ReplaySession) -> None:
        """Load and process session data for replay with streaming updates."""
        try:
            loop = asyncio.get_event_loop()

            # Load session data in thread pool (blocking FastF1 call)
            session = await loop.run_in_executor(
                None,
                lambda: session_manager.get_session(
                    replay.year, replay.race, replay.session_type
                ),
            )

            # Extract metadata immediately
            replay.event_name = session.event["EventName"]
            replay.session_name = session.name

            if replay.is_race:
                replay.total_laps = int(session.total_laps) if session.total_laps else None
                replay.estimated_total_laps = replay.total_laps

            # Build driver info
            if session.results is not None and not session.results.empty:
                for _, row in session.results.iterrows():
                    abbr = row.get("Abbreviation", "")
                    if abbr:
                        replay.drivers[abbr] = {
                            "number": str(row.get("DriverNumber", "")),
                            "name": f"{row.get('FirstName', '')} {row.get('LastName', '')}".strip(),
                            "team": row.get("TeamName", ""),
                            "color": f"#{row.get('TeamColor', '666666')}",
                        }

            # Switch to STREAMING status and notify (client can start rendering)
            replay.status = ReplayStatus.STREAMING
            await self._notify_subscribers(replay)
            logger.info(f"Replay {replay.replay_id} streaming: metadata loaded, building frames")

            # Build frames incrementally, notifying after each
            await self._build_timing_frames_streaming(replay, session)

            replay.status = ReplayStatus.READY
            await self._notify_subscribers(replay)
            logger.info(f"Replay {replay.replay_id} ready: {len(replay.frames)} frames")

        except Exception as e:
            replay.status = ReplayStatus.ERROR
            replay.error_message = str(e)
            await self._notify_subscribers(replay)
            logger.error(f"Error loading replay {replay.replay_id}: {e}")

    async def _build_timing_frames(self, replay: ReplaySession, session) -> None:
        """Build timing frames from session lap data."""
        laps = session.laps
        if laps is None or laps.empty:
            return

        # Get race control messages for flag status
        rcm = session.race_control_messages if hasattr(session, 'race_control_messages') else None

        # Group laps by LapNumber to create frames after each lap
        for lap_num in sorted(laps["LapNumber"].dropna().unique()):
            lap_data = laps[laps["LapNumber"] == lap_num].copy()
            if lap_data.empty:
                continue

            # Get session time from the first lap completion
            session_times = lap_data["Time"].dropna()
            if session_times.empty:
                continue

            frame_time = session_times.min()
            if pd.isna(frame_time):
                continue

            # Ensure frame_time is a timedelta
            if hasattr(frame_time, 'total_seconds'):
                frame_time_td = frame_time
            else:
                # Convert to timedelta if it's a different type
                frame_time_td = pd.to_timedelta(frame_time)

            # Calculate standings at this point
            entries = self._calculate_standings(replay, lap_data, laps, int(lap_num))

            if entries:
                # Get flag status from race control messages
                flag_status = "GREEN"
                message = None
                if rcm is not None and not rcm.empty:
                    # Convert rcm Time to timedelta for comparison
                    try:
                        rcm_times = pd.to_timedelta(rcm["Time"])
                        frame_seconds = frame_time_td.total_seconds()
                        relevant_mask = rcm_times.apply(
                            lambda x: x.total_seconds() <= frame_seconds if pd.notna(x) else False
                        )
                        relevant_msgs = rcm[relevant_mask]
                    except (TypeError, ValueError):
                        relevant_msgs = pd.DataFrame()

                    if not relevant_msgs.empty:
                        last_msg = relevant_msgs.iloc[-1]
                        if "Flag" in str(last_msg.get("Category", "")):
                            flag_text = str(last_msg.get("Flag", ""))
                            if "YELLOW" in flag_text.upper():
                                flag_status = "YELLOW"
                            elif "RED" in flag_text.upper():
                                flag_status = "RED"
                            elif "CHEQUERED" in flag_text.upper():
                                flag_status = "CHEQUERED"
                        message = last_msg.get("Message")

                frame = TimingFrame(
                    session_time=frame_time_td,
                    session_time_str=self._format_timedelta(frame_time_td),
                    entries=entries,
                    flag_status=flag_status,
                    message=message,
                )
                replay.frames.append(frame)

    async def _build_timing_frames_streaming(self, replay: ReplaySession, session) -> None:
        """Build timing frames incrementally, notifying subscribers after each frame."""
        laps = session.laps
        if laps is None or laps.empty:
            return

        # Get race control messages for flag status
        rcm = session.race_control_messages if hasattr(session, 'race_control_messages') else None

        # Get weather data if available
        weather_df = session.weather_data if hasattr(session, 'weather_data') else None

        # Group laps by LapNumber to create frames after each lap
        lap_numbers = sorted(laps["LapNumber"].dropna().unique())

        for lap_num in lap_numbers:
            lap_data = laps[laps["LapNumber"] == lap_num].copy()
            if lap_data.empty:
                continue

            # Get session time from the first lap completion
            session_times = lap_data["Time"].dropna()
            if session_times.empty:
                continue

            frame_time = session_times.min()
            if pd.isna(frame_time):
                continue

            # Ensure frame_time is a timedelta
            if hasattr(frame_time, 'total_seconds'):
                frame_time_td = frame_time
            else:
                frame_time_td = pd.to_timedelta(frame_time)

            # Calculate standings at this point
            entries = self._calculate_standings(replay, lap_data, laps, int(lap_num))

            if entries:
                # Get flag status and race control messages
                flag_status = "GREEN"
                message = None
                rc_messages = []

                if rcm is not None and not rcm.empty:
                    try:
                        rcm_times = pd.to_timedelta(rcm["Time"])
                        frame_seconds = frame_time_td.total_seconds()
                        relevant_mask = rcm_times.apply(
                            lambda x: x.total_seconds() <= frame_seconds if pd.notna(x) else False
                        )
                        relevant_msgs = rcm[relevant_mask]
                    except (TypeError, ValueError):
                        relevant_msgs = pd.DataFrame()

                    if not relevant_msgs.empty:
                        last_msg = relevant_msgs.iloc[-1]
                        if "Flag" in str(last_msg.get("Category", "")):
                            flag_text = str(last_msg.get("Flag", ""))
                            if "YELLOW" in flag_text.upper():
                                flag_status = "YELLOW"
                            elif "RED" in flag_text.upper():
                                flag_status = "RED"
                            elif "CHEQUERED" in flag_text.upper():
                                flag_status = "CHEQUERED"
                        message = last_msg.get("Message")

                        # Get recent race control messages (last 10)
                        recent_msgs = relevant_msgs.tail(10)
                        for _, msg_row in recent_msgs.iterrows():
                            msg_time = msg_row.get("Time")
                            time_str = self._format_timedelta(pd.to_timedelta(msg_time)) if pd.notna(msg_time) else ""
                            rc_messages.append(RaceControlMessage(
                                time=time_str,
                                message=str(msg_row.get("Message", "")),
                                category=str(msg_row.get("Category", "")),
                                flag=str(msg_row.get("Flag", "")) if pd.notna(msg_row.get("Flag")) else "",
                            ))

                # Get weather data for this frame time
                weather = None
                if weather_df is not None and not weather_df.empty:
                    try:
                        weather_times = pd.to_timedelta(weather_df["Time"])
                        frame_seconds = frame_time_td.total_seconds()
                        relevant_mask = weather_times.apply(
                            lambda x: x.total_seconds() <= frame_seconds if pd.notna(x) else False
                        )
                        relevant_weather = weather_df[relevant_mask]
                        if not relevant_weather.empty:
                            w = relevant_weather.iloc[-1]
                            weather = WeatherData(
                                air_temp=float(w["AirTemp"]) if pd.notna(w.get("AirTemp")) else None,
                                track_temp=float(w["TrackTemp"]) if pd.notna(w.get("TrackTemp")) else None,
                                wind_speed=float(w["WindSpeed"]) if pd.notna(w.get("WindSpeed")) else None,
                                wind_direction=int(w["WindDirection"]) if pd.notna(w.get("WindDirection")) else None,
                                rainfall=bool(w.get("Rainfall", False)),
                                humidity=float(w["Humidity"]) if pd.notna(w.get("Humidity")) else None,
                            )
                    except (TypeError, ValueError, KeyError):
                        pass

                frame = TimingFrame(
                    session_time=frame_time_td,
                    session_time_str=self._format_timedelta(frame_time_td),
                    entries=entries,
                    flag_status=flag_status,
                    message=message,
                    weather=weather,
                    race_control_messages=rc_messages,
                )
                replay.frames.append(frame)

                # Notify subscribers after each frame is added
                await self._notify_subscribers(replay)

                # Yield control to allow other tasks to run
                await asyncio.sleep(0)

    def _calculate_standings(
        self,
        replay: ReplaySession,
        current_lap_data: pd.DataFrame,
        all_laps: pd.DataFrame,
        lap_num: int,
    ) -> list[TimingEntry]:
        """Calculate standings and gaps for a specific lap."""
        entries = []

        # For each driver, get their most recent lap data up to this lap
        driver_standings = {}
        for driver in current_lap_data["Driver"].unique():
            driver_laps = all_laps[
                (all_laps["Driver"] == driver) & (all_laps["LapNumber"] <= lap_num)
            ]
            if driver_laps.empty:
                continue

            latest = driver_laps.iloc[-1]

            # Get tyre compound
            tyre_compound = None
            if "Compound" in latest.index and pd.notna(latest.get("Compound")):
                compound = str(latest["Compound"]).upper()
                if compound in ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET"):
                    tyre_compound = compound

            # Calculate tyre age (laps since last pit or session start)
            tyre_age = 0
            if "TyreLife" in latest.index and pd.notna(latest.get("TyreLife")):
                tyre_age = int(latest["TyreLife"])
            elif "Stint" in driver_laps.columns:
                current_stint = latest.get("Stint")
                if pd.notna(current_stint):
                    stint_laps = driver_laps[driver_laps["Stint"] == current_stint]
                    tyre_age = len(stint_laps)

            driver_standings[driver] = {
                "driver": driver,
                "lap": int(latest["LapNumber"]) if pd.notna(latest["LapNumber"]) else 0,
                "time": latest["Time"],
                "lap_time": latest["LapTime"],
                "position": int(latest["Position"]) if pd.notna(latest["Position"]) else 99,
                "is_pit": bool(latest.get("PitInTime") is not None and pd.notna(latest.get("PitInTime"))),
                "tyre_compound": tyre_compound,
                "tyre_age": tyre_age,
            }

        if not driver_standings:
            return entries

        # Sort by position (or by time for practice/quali)
        if replay.is_race:
            sorted_drivers = sorted(
                driver_standings.values(),
                key=lambda x: (x["position"], -x["lap"]),
            )
        else:
            # For practice/quali, sort by fastest lap time
            for driver, data in driver_standings.items():
                driver_best = all_laps[
                    (all_laps["Driver"] == driver) & (all_laps["LapNumber"] <= lap_num)
                ]["LapTime"].min()
                data["best_time"] = driver_best

            sorted_drivers = sorted(
                driver_standings.values(),
                key=lambda x: x.get("best_time", pd.Timedelta.max),
            )

        # Calculate gaps
        leader = sorted_drivers[0] if sorted_drivers else None

        for pos, data in enumerate(sorted_drivers, 1):
            driver = data["driver"]
            driver_info = replay.drivers.get(driver, {})

            if replay.is_race:
                # Race: gap in laps or seconds
                if pos == 1:
                    gap = f"Lap {data['lap']}"
                else:
                    lap_diff = leader["lap"] - data["lap"]
                    if lap_diff > 0:
                        gap = f"+{lap_diff} LAP{'S' if lap_diff > 1 else ''}"
                    elif leader["time"] and data["time"]:
                        time_diff = data["time"] - leader["time"]
                        gap = f"+{time_diff.total_seconds():.3f}"
                    else:
                        gap = "-"
            else:
                # Practice/Quali: show best lap time for leader, delta for others
                if pos == 1:
                    if data.get("best_time") and pd.notna(data["best_time"]):
                        gap = self._format_lap_time(data["best_time"])
                    else:
                        gap = "-"
                else:
                    if leader.get("best_time") and data.get("best_time"):
                        if pd.notna(leader["best_time"]) and pd.notna(data["best_time"]):
                            delta = data["best_time"] - leader["best_time"]
                            gap = f"+{delta.total_seconds():.3f}"
                        else:
                            gap = "-"
                    else:
                        gap = "-"

            # Calculate interval to car ahead
            if pos == 1:
                interval = "-"
            elif pos <= len(sorted_drivers):
                prev = sorted_drivers[pos - 2]
                if replay.is_race:
                    if prev["time"] and data["time"]:
                        int_diff = data["time"] - prev["time"]
                        interval = f"+{int_diff.total_seconds():.3f}"
                    else:
                        interval = "-"
                else:
                    if prev.get("best_time") and data.get("best_time"):
                        if pd.notna(prev["best_time"]) and pd.notna(data["best_time"]):
                            int_diff = data["best_time"] - prev["best_time"]
                            interval = f"+{int_diff.total_seconds():.3f}"
                        else:
                            interval = "-"
                    else:
                        interval = "-"
            else:
                interval = "-"

            # Format last lap time
            last_lap_str = None
            if data["lap_time"] and pd.notna(data["lap_time"]):
                last_lap_str = self._format_lap_time(data["lap_time"])

            entry = TimingEntry(
                position=pos,
                driver=driver,
                driver_number=driver_info.get("number", ""),
                team=driver_info.get("team", ""),
                team_color=driver_info.get("color", "#666666"),
                gap=gap,
                interval=interval,
                lap=data["lap"],
                last_lap_time=last_lap_str,
                is_pit=data.get("is_pit", False),
                tyre_compound=data.get("tyre_compound"),
                tyre_age=data.get("tyre_age", 0),
            )
            entries.append(entry)

        return entries

    def _format_timedelta(self, td: timedelta) -> str:
        """Format timedelta as HH:MM:SS.mmm."""
        if pd.isna(td):
            return "00:00:00.000"
        total_seconds = td.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"

    def _format_lap_time(self, td: timedelta) -> str:
        """Format lap time as M:SS.mmm."""
        if pd.isna(td):
            return "-"
        total_seconds = td.total_seconds()
        minutes = int(total_seconds // 60)
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:06.3f}"

    async def get_replay(self, replay_id: str) -> Optional[ReplaySession]:
        """Get a replay session by ID."""
        return self._replays.get(replay_id)

    def get_current_state(self, replay_id: str) -> dict[str, Any]:
        """Get current state including frame data (for control responses)."""
        replay = self._replays.get(replay_id)
        if not replay:
            return {"error": "Replay not found"}
        return self._frame_to_dict(replay)

    async def set_speed(self, replay_id: str, speed: float) -> bool:
        """Set replay playback speed, recalibrating the virtual clock."""
        replay = self._replays.get(replay_id)
        if not replay:
            return False

        old_speed = replay.speed
        new_speed = max(0.5, min(speed, 20.0))  # Clamp between 0.5x and 20x

        # If playing, recalibrate the virtual clock to maintain current position
        if replay.status == ReplayStatus.PLAYING and replay.playback_start_real:
            current_session_time = replay.get_current_session_time()
            if current_session_time:
                replay.playback_start_real = datetime.utcnow()
                replay.playback_start_session = current_session_time

        replay.speed = new_speed
        return True

    async def play(self, replay_id: str) -> bool:
        """Start or resume playback."""
        replay = self._replays.get(replay_id)
        # Allow playing during STREAMING (frames still loading), READY, or PAUSED
        if not replay or replay.status not in (
            ReplayStatus.STREAMING, ReplayStatus.READY, ReplayStatus.PAUSED
        ):
            return False

        # Initialize or resume the virtual clock
        if replay.current_frame_index < len(replay.frames):
            current_frame = replay.frames[replay.current_frame_index]
            replay.playback_start_session = current_frame.session_time
        else:
            replay.playback_start_session = timedelta(0)
        replay.playback_start_real = datetime.utcnow()

        replay.status = ReplayStatus.PLAYING

        # Start playback task if not already running
        if replay_id not in self._replay_tasks or self._replay_tasks[replay_id].done():
            self._replay_tasks[replay_id] = asyncio.create_task(
                self._playback_loop(replay)
            )

        return True

    async def pause(self, replay_id: str) -> bool:
        """Pause playback, preserving the current session time."""
        replay = self._replays.get(replay_id)
        if not replay or replay.status != ReplayStatus.PLAYING:
            return False

        # Save the current session time before pausing
        if replay.playback_start_real:
            current_session_time = replay.get_current_session_time()
            if current_session_time:
                replay.playback_start_session = current_session_time

        replay.playback_start_real = None  # Stop the clock
        replay.status = ReplayStatus.PAUSED
        return True

    async def seek(self, replay_id: str, frame_index: int) -> bool:
        """Seek to a specific frame."""
        replay = self._replays.get(replay_id)
        if not replay or len(replay.frames) == 0:
            return False

        replay.current_frame_index = max(0, min(frame_index, len(replay.frames) - 1))

        # Update virtual clock to match the new frame position
        new_frame = replay.frames[replay.current_frame_index]
        if replay.status == ReplayStatus.PLAYING:
            # If playing, reset the clock to current position
            replay.playback_start_real = datetime.utcnow()
            replay.playback_start_session = new_frame.session_time
        else:
            # If paused, just update the session time offset
            replay.playback_start_session = new_frame.session_time

        await self._notify_subscribers(replay)
        return True

    async def seek_by_time(self, replay_id: str, delta_seconds: float) -> bool:
        """Seek forward/backward by a time delta (negative = backward)."""
        replay = self._replays.get(replay_id)
        if not replay or len(replay.frames) == 0:
            return False

        # Get current session time
        if replay.current_frame_index < len(replay.frames):
            current_time = replay.frames[replay.current_frame_index].session_time
        else:
            current_time = replay.frames[-1].session_time

        # Calculate target time
        target_time = current_time + timedelta(seconds=delta_seconds)

        # Find the frame closest to the target time
        best_index = 0
        best_diff = float('inf')
        for i, frame in enumerate(replay.frames):
            diff = abs((frame.session_time - target_time).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_index = i

        replay.current_frame_index = best_index

        # Update virtual clock to match the new frame position
        new_frame = replay.frames[replay.current_frame_index]
        if replay.status == ReplayStatus.PLAYING:
            replay.playback_start_real = datetime.utcnow()
            replay.playback_start_session = new_frame.session_time
        else:
            replay.playback_start_session = new_frame.session_time

        await self._notify_subscribers(replay)
        return True

    async def _playback_loop(self, replay: ReplaySession) -> None:
        """Main playback loop using virtual clock for accurate timing."""
        try:
            while replay.status == ReplayStatus.PLAYING:
                if replay.current_frame_index >= len(replay.frames):
                    replay.status = ReplayStatus.COMPLETED
                    await self._notify_subscribers(replay, final=True)
                    break

                # Get current session time from virtual clock
                current_session_time = replay.get_current_session_time()
                if current_session_time is None:
                    await asyncio.sleep(0.1)
                    continue

                # Find frames that should be displayed based on session time
                while (replay.current_frame_index < len(replay.frames) and
                       replay.status == ReplayStatus.PLAYING):

                    frame = replay.frames[replay.current_frame_index]
                    time_until_frame = (frame.session_time - current_session_time).total_seconds()

                    if time_until_frame <= 0:
                        # Frame is due, send it
                        await self._notify_subscribers(replay)
                        replay.current_frame_index += 1

                        # Update current session time
                        current_session_time = replay.get_current_session_time()
                        if current_session_time is None:
                            break
                    else:
                        # Wait until next frame is due (check frequently for status changes)
                        wait_time = min(time_until_frame / replay.speed, 0.5)
                        await asyncio.sleep(max(0.05, wait_time))

                        # Refresh current time after wait
                        current_session_time = replay.get_current_session_time()
                        if current_session_time is None:
                            break
                        break  # Re-check the while condition

        except Exception as e:
            logger.error(f"Playback error for {replay.replay_id}: {e}")
            replay.status = ReplayStatus.ERROR
            replay.error_message = str(e)

    async def subscribe(self, replay_id: str) -> asyncio.Queue:
        """Subscribe to replay updates."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        async with self._lock:
            if replay_id not in self._subscribers:
                self._subscribers[replay_id] = []
            self._subscribers[replay_id].append(queue)

            # Send current state
            replay = self._replays.get(replay_id)
            if replay:
                await queue.put(self._frame_to_dict(replay))

        return queue

    async def unsubscribe(self, replay_id: str, queue: asyncio.Queue) -> None:
        """Unsubscribe from replay updates."""
        async with self._lock:
            if replay_id in self._subscribers:
                try:
                    self._subscribers[replay_id].remove(queue)
                except ValueError:
                    pass

    async def _notify_subscribers(
        self, replay: ReplaySession, final: bool = False
    ) -> None:
        """Send update to all subscribers."""
        async with self._lock:
            queues = self._subscribers.get(replay.replay_id, [])
            data = self._frame_to_dict(replay, final)

            for queue in queues:
                try:
                    await queue.put(data)
                except asyncio.QueueFull:
                    pass

    def _frame_to_dict(
        self, replay: ReplaySession, final: bool = False
    ) -> dict[str, Any]:
        """Convert current frame to dict for SSE."""
        frame = None
        if 0 <= replay.current_frame_index < len(replay.frames):
            f = replay.frames[replay.current_frame_index]

            # Build weather dict if available
            weather_dict = None
            if f.weather:
                weather_dict = {
                    "air_temp": f.weather.air_temp,
                    "track_temp": f.weather.track_temp,
                    "wind_speed": f.weather.wind_speed,
                    "wind_direction": f.weather.wind_direction,
                    "rainfall": f.weather.rainfall,
                    "humidity": f.weather.humidity,
                }

            # Build race control messages
            rc_messages = [
                {
                    "time": m.time,
                    "message": m.message,
                    "category": m.category,
                    "flag": m.flag,
                }
                for m in f.race_control_messages
            ]

            frame = {
                "session_time": f.session_time_str,
                "flag_status": f.flag_status,
                "message": f.message,
                "weather": weather_dict,
                "race_control_messages": rc_messages,
                "positions": [
                    {
                        "pos": e.position,
                        "driver": e.driver,
                        "number": e.driver_number,
                        "team": e.team,
                        "color": e.team_color,
                        "gap": e.gap,
                        "interval": e.interval,
                        "lap": e.lap,
                        "last_lap": e.last_lap_time,
                        "pit": e.is_pit,
                        "out": e.is_out,
                        "tyre": e.tyre_compound,
                        "tyre_age": e.tyre_age,
                    }
                    for e in f.entries
                ],
            }

        return {
            "replay_id": replay.replay_id,
            "status": replay.status.value,
            "speed": replay.speed,
            "event_name": replay.event_name,
            "session_name": replay.session_name,
            "is_race": replay.is_race,
            "total_laps": replay.total_laps,
            "estimated_total_laps": replay.estimated_total_laps,
            "current_frame": replay.current_frame_index,
            "total_frames": len(replay.frames),
            "frames_loaded": len(replay.frames),
            "frame": frame,
            "final": final,
            "error": replay.error_message,
        }

    async def cleanup_old_replays(self, max_age_hours: int = 4) -> int:
        """Remove old replay sessions."""
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        removed = 0

        async with self._lock:
            for replay_id in list(self._replays.keys()):
                replay = self._replays[replay_id]
                if replay.created_at < cutoff:
                    # Cancel playback task if running
                    if replay_id in self._replay_tasks:
                        self._replay_tasks[replay_id].cancel()
                        del self._replay_tasks[replay_id]

                    del self._replays[replay_id]
                    if replay_id in self._subscribers:
                        del self._subscribers[replay_id]
                    removed += 1

        return removed


# Global instance
replay_manager = ReplayManager()
