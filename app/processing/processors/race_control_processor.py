"""
Race Control Processor — race control messages, sector flags, driver flags.

Subscribes to: RaceControlMessages
Emits:
  - raceControlMessages  (timestamp, color, message)
  - yellowFlag           (list of sectors currently under yellow)
  - driverFlag           (driver number, flag colour)

Tracks last seen message index to avoid re-processing accumulated history.
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _flag_to_color(flag: str) -> str:
    flag = flag.upper()
    if flag == "RED":
        return "red"
    if flag in ("YELLOW", "DOUBLE YELLOW"):
        return "yellow"
    if flag in ("GREEN", "CLEAR"):
        return "green"
    if flag == "CHEQUERED":
        return "chequered"
    if flag == "BLUE":
        return "blue"
    if flag == "BLACK AND WHITE":
        return "blackAndWhite"
    return flag.lower()


# Keyword tests for penalty-related race-control messages so the tile
# can display them in orange (race only — F1's RCM message text covers
# all common penalty types).
_PENALTY_KEYWORDS = (
    "PENALTY",       # catches "5 SECOND PENALTY", "TIME PENALTY", etc.
    "DRIVE THROUGH",
    "STOP AND GO",
    "STOP/GO",
    "GRID DROP",
    "GRID PENALTY",
    "REPRIMAND",
)


def _is_penalty_message(message: str) -> bool:
    if not message:
        return False
    upper = message.upper()
    return any(kw in upper for kw in _PENALTY_KEYWORDS)


def _other_message_color(message: str) -> str:
    """Display colour for Category='Other' RCM (stewards / penalty text).

    F1 gives these no structured field, so they're matched on the message:
      UNDER INVESTIGATION                  → yellow (active, in-session)
      penalty awarded (not noted/served)   → orange
      everything else                      → clear
        (WILL BE INVESTIGATED = post-session, no in-session penalty; NOTED =
         record only; PENALTY ... SERVED = after-the-fact; plus track limits,
         track surface, recovery, weather, session-info messages)
    """
    upper = (message or "").upper()
    if "UNDER INVESTIGATION" in upper:
        return "yellow"
    if (_is_penalty_message(message)
            and "NOTED" not in upper and "SERVED" not in upper
            and "WILL BE INVESTIGATED" not in upper):
        return "orange"
    return "clear"


class RaceControlProcessor(Processor):
    """Processes race control messages into display topics."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._yellow_sectors: set[int] = set()
        self._last_key: int = -1
        self._all_messages: list[dict] = []  # Accumulated for cumulative emit

    def subscribe(self) -> None:
        self._bus.on("RaceControlMessages", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        messages = data.get("Messages") or data
        if not isinstance(messages, (dict, list)):
            return

        if isinstance(messages, dict):
            # Dict keyed by index — only process new entries
            for key_str, msg in sorted(messages.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                if not isinstance(msg, dict):
                    continue
                try:
                    idx = int(key_str)
                except (ValueError, TypeError):
                    idx = 0
                if idx <= self._last_key:
                    continue
                self._last_key = idx
                self._process_msg(msg, clock_time)
        else:
            # List — track by position index
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if i <= self._last_key:
                    continue
                self._last_key = i
                self._process_msg(msg, clock_time)

    def _process_msg(self, msg: dict, clock_time: datetime) -> None:
        category = msg.get("Category", "")
        timestamp = msg.get("Utc", "")
        message = msg.get("Message", "")
        scope = (msg.get("Scope") or "").upper()
        flag = msg.get("Flag", "")

        # raceControlMessages — emit full accumulated list each time.
        # Colour is fully server-computed: Flag carries the flag colour,
        # SafetyCar is yellow, and "Other" (stewards/penalties — no structured
        # field) is text-matched (see _other_message_color).
        if category == "Flag":
            color = _flag_to_color(flag)
        elif category == "SafetyCar":
            color = "yellow"
        else:
            color = _other_message_color(message)

        self._all_messages.append({
            "timestamp": timestamp,
            "color": color,
            "message": message,
        })
        self._bus.emit("raceControlMessages", list(self._all_messages), clock_time)

        # Sector flags
        if category == "Flag":
            if scope == "SECTOR":
                sector = msg.get("Sector")
                if sector is not None:
                    sector = int(sector)
                    flag_upper = flag.upper()
                    if flag_upper in ("CLEAR", "GREEN"):
                        self._yellow_sectors.discard(sector)
                    elif flag_upper in ("YELLOW", "DOUBLE YELLOW"):
                        self._yellow_sectors.add(sector)
                self._bus.emit("yellowFlag",
                              sorted(self._yellow_sectors), clock_time)

            elif scope == "TRACK":
                flag_upper = flag.upper()
                if flag_upper in ("CLEAR", "GREEN"):
                    self._yellow_sectors.clear()
                    self._bus.emit("yellowFlag", [], clock_time)

            elif scope == "DRIVER":
                racing_number = msg.get("RacingNumber", "")
                if racing_number:
                    self._bus.emit("driverFlag", {
                        "driverNumber": racing_number,
                        "flag": _flag_to_color(flag),
                    }, clock_time)
