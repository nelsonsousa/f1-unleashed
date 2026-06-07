"""FIA Stewards processor — race/sprint only.

Maintains a single ordered stack of "indicators" that apply to one or
more drivers (= investigations, awarded penalties, deferred reviews,
notes, plus the track-limits black-and-white flag and the waved blue
flag). Emits the `fiaStewards` topic on every change; the standings
tile subscribes to render per-driver badges.

Indicator object shape (= what's stored in the stack and emitted):

    {
        "id":          int,           # unique, monotonically increasing
        "kind":        str,           # see KINDS below
        "driverNums":  list[str],     # car numbers this indicator applies to
        "label":       str,           # text shown on the badge ("PEN" / "+5s" / ...)
        "color":       str,           # yellow / white / red / blue / trackLimits / black
        "reason":      str | None,    # trailing infringement text from the RCM
        "tooltip":     str,           # full description for hover
        "incidentKey": tuple | None,  # (sorted_cars, reason) for multi-driver resolution
        "tsMs":        int | None,    # session-clock ms of issuance
        "untilMs":     int | None,    # session-clock ms of expiry (blue flag only)
    }

KINDS:
    "investigation"  — under investigation                 (yellow PEN)
    "deferred"       — will be investigated after the race (white  PEN)
    "noted"          — incident noted, awaiting verdict    (white  PEN)
    "Ns"             — N-second time penalty (5s, 10s, …)  (red   +Ns)
    "dt"             — drive-through penalty               (red   D-T)
    "sg"             — stop-and-go penalty                 (red   S&G)
    "blackFlag"      — disqualification                    (= DSQ row override on the frontend)
    "trackLimits"    — black-and-white flag, track limits  (track-limits flag SVG)
    "blueFlag"       — waved blue flag                     (blue flag SVG, 10 s)

Order in the stack matches chronological insertion. The frontend may
re-sort per-driver into: penalties (red) → investigations (yellow) →
white → track-limits flag → blue flag.
"""

import logging
import re
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)

# Sessions for which this processor is enabled. Practice + qualifying
# variants get nothing — investigations and penalties don't apply.
_ACTIVE_SESSION_TYPES = {"race", "sprint"}


class FiaStewardsProcessor(Processor):
    # Match any number-then-(TLA) pattern — works for all variants:
    #   "CAR 27 (HUL)"         → "27"
    #   "CARS 30 (LAW) AND 27 (HUL)"  → "30", "27"
    #   "CARS 5 (BOR), 77 (BOT) AND 10 (GAS)"  → "5", "77", "10"
    _CAR_RX = re.compile(r"\b(\d+)\s*\([A-Z]{3}\)")
    _TIME_PEN_RX = re.compile(
        r"(\d+)\s*SECOND(?:S)?\s*(?:TIME\s*)?PENALTY",
        re.I,
    )
    _BLUE_FLAG_EXPIRY_MS = 10_000   # SME spec — blue flag shown for 10 s

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._active = session_type.lower() in _ACTIVE_SESSION_TYPES
        self._stack: list[dict] = []
        self._next_id: int = 1
        self._last_rcm_index: int = -1
        self._start_time: Optional[datetime] = None

    def subscribe(self) -> None:
        if not self._active:
            return
        self._bus.on("RaceControlMessages", self._handle_rcm)
        self._bus.on("SessionInfo", self._handle_session_info)

    # ── Snapshot / restore ─────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "stack": list(self._stack),
            "next_id": self._next_id,
            "last_rcm_index": self._last_rcm_index,
        }

    def restore(self, state: dict) -> None:
        self._stack = list(state.get("stack") or [])
        self._next_id = int(state.get("next_id") or 1)
        self._last_rcm_index = int(state.get("last_rcm_index") or -1)

    def reset(self) -> None:
        self._stack.clear()
        self._next_id = 1
        self._last_rcm_index = -1
        self._start_time = None

    # ── Handlers ───────────────────────────────────────────────────────

    def _handle_session_info(self, data: Any, clock_time: datetime) -> None:
        # Need the session start so we can compute session-clock offsets
        # for blue-flag expiry. The first SessionInfo message gives us
        # the reference point.
        if self._start_time is None and clock_time is not None:
            self._start_time = clock_time

    def _handle_rcm(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        messages = data.get("Messages")
        if isinstance(messages, dict):
            for key_str, msg in sorted(
                messages.items(),
                key=lambda x: int(x[0]) if x[0].isdigit() else 0,
            ):
                if not isinstance(msg, dict):
                    continue
                try:
                    idx = int(key_str)
                except (ValueError, TypeError):
                    idx = 0
                if idx <= self._last_rcm_index:
                    continue
                self._last_rcm_index = idx
                self._process(msg, clock_time)
        elif isinstance(messages, list):
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if i <= self._last_rcm_index:
                    continue
                self._last_rcm_index = i
                self._process(msg, clock_time)

    # ── Parsing helpers ────────────────────────────────────────────────

    @classmethod
    def _extract_cars(cls, text: str) -> list[str]:
        seen: list[str] = []
        for m in cls._CAR_RX.finditer(text):
            n = m.group(1)
            if n not in seen:
                seen.append(n)
        return seen

    @staticmethod
    def _extract_reason(text: str) -> Optional[str]:
        if " - " not in text:
            return None
        tail = text.rsplit(" - ", 1)[-1].strip()
        tail = re.sub(r"\s*\(\d+:\d+:\d+\)\s*$", "", tail)
        return tail or None

    def _session_ms(self, clock_time: Optional[datetime]) -> Optional[int]:
        if clock_time is None or self._start_time is None:
            return None
        return int((clock_time - self._start_time).total_seconds() * 1000)

    # ── State mutations ────────────────────────────────────────────────

    def _push(self, indicator: dict) -> None:
        indicator["id"] = self._next_id
        self._next_id += 1
        self._stack.append(indicator)

    def _remove_matching(
        self,
        predicate,
    ) -> bool:
        """Drop indicators matching `predicate(ind) -> bool`. Returns
        True if anything was removed."""
        before = len(self._stack)
        self._stack = [i for i in self._stack if not predicate(i)]
        return len(self._stack) != before

    def _emit(self, clock_time: datetime) -> None:
        # Strip expired blue-flag indicators based on the current
        # session-ms BEFORE serialising — keeps the wire payload clean.
        now_ms = self._session_ms(clock_time)
        if now_ms is not None:
            self._remove_matching(
                lambda i: i.get("kind") == "blueFlag"
                and (i.get("untilMs") is not None)
                and (i["untilMs"] < now_ms)
            )
        self._bus.emit("fiaStewards", {"stack": list(self._stack)}, clock_time)

    # ── Per-message handler ────────────────────────────────────────────

    def _process(self, msg: dict, clock_time: datetime) -> None:
        text = msg.get("Message", "") or ""
        category = msg.get("Category", "") or ""
        flag = msg.get("Flag", "") or ""
        upper = text.upper()
        cars = self._extract_cars(text)
        reason = self._extract_reason(text)
        now_ms = self._session_ms(clock_time)
        changed = False

        # ── Flag categories ────────────────────────────────────────────
        if category == "Flag":
            primary = cars[0] if cars else msg.get("RacingNumber")
            if primary is None:
                return
            primary = str(primary)

            if flag == "BLACK":
                # DSQ — wipe ALL other indicators for this driver and
                # push a single blackFlag indicator.
                changed |= self._remove_matching(
                    lambda i: primary in (i.get("driverNums") or [])
                )
                self._push({
                    "kind": "blackFlag",
                    "driverNums": [primary],
                    "label": "DSQ",
                    "color": "black",
                    "reason": reason,
                    "tooltip": "DISQUALIFIED",
                    "incidentKey": None,
                    "tsMs": now_ms,
                    "untilMs": None,
                })
                self._emit(clock_time)
                return

            if flag == "BLACK AND WHITE" and "TRACK LIMITS" in upper:
                # Only one track-limits indicator per driver — replace if
                # already present (= the warning state simply persists).
                already = any(
                    i.get("kind") == "trackLimits"
                    and primary in (i.get("driverNums") or [])
                    for i in self._stack
                )
                if not already:
                    self._push({
                        "kind": "trackLimits",
                        "driverNums": [primary],
                        "label": "",
                        "color": "trackLimits",
                        "reason": "TRACK LIMITS",
                        "tooltip": "BLACK AND WHITE — TRACK LIMITS",
                        "incidentKey": None,
                        "tsMs": now_ms,
                        "untilMs": None,
                    })
                    self._emit(clock_time)
                return

            if flag == "BLUE" or "WAVED BLUE FLAG" in upper:
                if now_ms is None:
                    return
                # Refresh existing blue-flag entry for this driver if
                # present (= reset the 10 s timer); otherwise push.
                refreshed = False
                for ind in self._stack:
                    if ind.get("kind") == "blueFlag" \
                            and primary in (ind.get("driverNums") or []):
                        ind["untilMs"] = now_ms + self._BLUE_FLAG_EXPIRY_MS
                        refreshed = True
                        break
                if not refreshed:
                    self._push({
                        "kind": "blueFlag",
                        "driverNums": [primary],
                        "label": "",
                        "color": "blue",
                        "reason": None,
                        "tooltip": "BLUE FLAG",
                        "incidentKey": None,
                        "tsMs": now_ms,
                        "untilMs": now_ms + self._BLUE_FLAG_EXPIRY_MS,
                    })
                self._emit(clock_time)
                return
            return

        # ── FIA STEWARDS messages ──────────────────────────────────────
        if "FIA STEWARDS" not in upper:
            return
        if not cars:
            return
        incident_key = (tuple(sorted(cars)), reason)

        # Resolutions: clear matching investigations/deferred.
        if "NO FURTHER ACTION" in upper or "NO FURTHER INVESTIGATION" in upper:
            cars_set = set(cars)
            changed |= self._remove_matching(
                lambda i: i.get("kind") in ("investigation", "deferred", "noted")
                and i.get("reason") == reason
                and bool(set(i.get("driverNums") or []) & cars_set)
            )
            if changed:
                self._emit(clock_time)
            return

        if "PENALTY SERVED" in upper:
            cars_set = set(cars)
            # Penalty-served clears any awarded-penalty indicator that
            # involves any of the named cars + the same reason.
            changed |= self._remove_matching(
                lambda i: i.get("kind") in ("dt", "sg")
                or (isinstance(i.get("kind"), str)
                    and i["kind"].endswith("s")
                    and i["kind"][:-1].isdigit())
                if i.get("reason") == reason
                and bool(set(i.get("driverNums") or []) & cars_set)
                else False
            )
            if changed:
                self._emit(clock_time)
            return

        cars_set = set(cars)

        def _supersede_open(predicate_kind_set):
            """Remove existing indicators with kind in `predicate_kind_set`
            that share the same reason AND at least one car with this
            incident — state transition (= old indicator replaced)."""
            self._remove_matching(
                lambda i: i.get("kind") in predicate_kind_set
                and i.get("reason") == reason
                and bool(set(i.get("driverNums") or []) & cars_set)
            )

        if "UNDER INVESTIGATION" in upper:
            # Dedup: same exact incident already in progress.
            already = any(
                i.get("kind") == "investigation"
                and i.get("reason") == reason
                and set(i.get("driverNums") or []) == cars_set
                for i in self._stack
            )
            if not already:
                self._push({
                    "kind": "investigation",
                    "driverNums": cars,
                    "label": "PEN",
                    "color": "yellow",
                    "reason": reason,
                    "tooltip": f"UNDER INVESTIGATION: {reason}".rstrip(": "),
                    "incidentKey": incident_key,
                    "tsMs": now_ms,
                    "untilMs": None,
                })
            self._emit(clock_time)
            return

        if "WILL BE INVESTIGATED AFTER" in upper:
            # Per SME 2026-06-07: deferred reviews don't affect the race
            # (any verdict comes after the chequered) so we drop them
            # entirely. Just clear any related open investigation /
            # noted for the involved cars and exit.
            _supersede_open({"investigation", "noted"})
            self._emit(clock_time)
            return

        if " NOTED" in f" {upper} " or upper.endswith(" NOTED"):
            # "noted" supersedes a prior investigation.
            _supersede_open({"investigation"})
            self._push({
                "kind": "noted",
                "driverNums": cars,
                "label": "PEN",
                "color": "white",
                "reason": reason,
                "tooltip": f"NOTED: {reason}".rstrip(": "),
                "incidentKey": incident_key,
                "tsMs": now_ms,
                "untilMs": None,
            })
            self._emit(clock_time)
            return

        # Specific awarded penalties.
        pen_kind: Optional[str] = None
        label: Optional[str] = None
        tooltip_prefix: Optional[str] = None
        if m := self._TIME_PEN_RX.search(upper):
            n = m.group(1)
            pen_kind = f"{n}s"
            label = f"+{n}s"
            tooltip_prefix = f"{n} SECOND PENALTY"
        elif "DRIVE THROUGH" in upper:
            pen_kind = "dt"
            label = "D-T"
            tooltip_prefix = "DRIVE THROUGH PENALTY"
        elif "STOP-AND-GO" in upper or "STOP AND GO" in upper:
            pen_kind = "sg"
            label = "S&G"
            tooltip_prefix = "STOP AND GO PENALTY"

        if pen_kind is None:
            return

        # Penalty awarded → the matching investigation / deferred / noted
        # is resolved. Clears ALL such indicators with the same reason
        # that involve ANY car in this incident.
        named_cars = set(cars)
        self._remove_matching(
            lambda i: i.get("kind") in ("investigation", "deferred", "noted")
            and i.get("reason") == reason
            and bool(set(i.get("driverNums") or []) & named_cars)
        )

        self._push({
            "kind": pen_kind,
            "driverNums": cars,
            "label": label,
            "color": "red",
            "reason": reason,
            "tooltip": f"{tooltip_prefix}: {reason}".rstrip(": "),
            "incidentKey": incident_key,
            "tsMs": now_ms,
            "untilMs": None,
        })
        # If the penalty's reason is TRACK LIMITS, the related track-
        # limits flag for any of the named cars is cleared.
        if reason and "TRACK LIMITS" in reason.upper():
            self._remove_matching(
                lambda i: i.get("kind") == "trackLimits"
                and bool(set(i.get("driverNums") or []) & named_cars)
            )
        self._emit(clock_time)
