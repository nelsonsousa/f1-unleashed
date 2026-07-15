"""Live-dashboard per-driver info tile (Practice/Qualifying) — server-computed (card 277 / Z4PfDRry).

Two modes, driven by lapPrediction:
  PREDICTING (a live prediction, projectedMs present) → show the projected lap time + predicted
    position (colours from the prediction).
  OBSERVED (lapPrediction status='lapEnd') → hold the last predicted values, then switch to the
    ACTUAL lap time (the NEXT driverLaps) and ACTUAL position (the NEXT standings), updating on
    every later message until the next prediction. The switch reacts to the NEXT message of the
    now-active listener — it does not recover its last message. (user 2026-07-15)

Stopwatch is ALWAYS the current lap's running time — the client ticks its own S/F anchor, so it
resets to 0 each lap; it's blank only in PIT/STOP. Classification → a colour circle
(PUSH green / SLOW·OUT white / PIT orange / STOP red).

Emits dashInfo:{num} = { running, lapTime:{ms,dp,colour}|null, position:{text,colour}|null, flag }.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.processing.processors.base import Processor


def _int_ms(s: Any) -> Optional[int]:
    """Interval string ("+0.834") → ms; None for lapped/non-numeric ("+1 LAP", "")."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or "L" in s.upper():        # "1 LAP" / "LAP" — lapped, not a sub-second gap
        return None
    body = s.lstrip("+-").strip()
    if not body:
        return None
    try:
        if ":" in body:
            mm, rest = body.split(":", 1)
            return int(round((int(mm) * 60 + float(rest)) * 1000))
        return int(round(float(body) * 1000))
    except (ValueError, IndexError):
        return None


def _lap_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    try:
        if ":" in s:
            m, rest = s.split(":", 1)
            return int(round((int(m) * 60 + float(rest)) * 1000))
        return int(round(float(s) * 1000))
    except (ValueError, IndexError):
        return None


class DashboardInfoProcessor(Processor):
    def __init__(self, bus, session_type: str):
        super().__init__(bus, session_type)
        self._is_pq = session_type in ("practice", "qualifying")
        self._is_race = session_type in ("race", "sprint")
        # ── race state (card J3V1CFdS) ──
        self._int_ms: dict[str, Optional[int]] = {}     # interval-to-car-ahead ms
        self._int_text: dict[str, Optional[str]] = {}   # interval string ("+0.834")
        self._int_colour: dict[str, str] = {}           # driverInt.trend (Int colour band)
        self._r_status: dict[str, Optional[str]] = {}   # driverStatus
        self._r_pos: dict[str, Optional[int]] = {}      # standings position
        self._tyre: dict[str, dict] = {}                # num -> {compound, isNew, age}
        self._mode: dict[str, Optional[str]] = {}      # num -> "predicting" | "observed" | None
        self._lap_time: dict[str, Optional[dict]] = {}  # {ms, dp, colour}
        self._position: dict[str, Optional[dict]] = {}  # {text, colour}
        self._status: dict[str, str] = {}
        self._cls: dict[str, str] = {}                  # current lap classification type
        self._best_ms: dict[str, int] = {}             # best-this-part ms
        self._pos: dict[str, int] = {}                 # standings position
        self._best_colour: dict[str, str] = {}         # num -> current best-lap band colour
        self._obs_colour: dict[str, str] = {}          # num -> colour of the observed lap (time + pos)
        self._obs_pb: dict[str, bool] = {}             # num -> the observed lap was a personal best
        self._last: dict[str, dict] = {}               # dedup

    def subscribe(self) -> None:
        if self._is_pq:
            self._bus.on("*", self._wild)
            self._bus.on("standings", self._on_standings)
        elif self._is_race:
            self._bus.on("*", self._wild_race)
            self._bus.on("standings", self._on_standings_race)

    # ── race inputs (card J3V1CFdS) ─────────────────────────────────────────
    def _wild_race(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverInt:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict):
                self._int_text[num] = data.get("interval")
                self._int_ms[num] = _int_ms(data.get("interval"))
                self._int_colour[num] = data.get("trend") or ""
                self._emit_race(num, clock_time)
        elif topic.startswith("driverStatus:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, str):
                self._r_status[num] = data
                self._emit_race(num, clock_time)
        elif topic.startswith("currentTyre:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict) and data.get("compound"):
                self._tyre[num] = {"compound": data.get("compound"),
                                   "isNew": data.get("isNew"), "age": data.get("age")}
                self._emit_race(num, clock_time)

    def _on_standings_race(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for d in data.get("drivers", []):
            if isinstance(d, dict) and "num" in d and "position" in d:
                num = str(d["num"])
                self._r_pos[num] = d["position"]
                self._emit_race(num, clock_time)

    def _emit_race(self, num: str, clock_time: datetime) -> None:
        status = self._r_status.get(num)
        int_ms = self._int_ms.get(num)
        # Indicator: PIT → orange (priority); else green when the interval is under 1 s. OUT is not
        # marked. (SME 2026-07-15)
        if status == "PIT":
            indicator = "orange"
        elif int_ms is not None and int_ms < 1000:
            indicator = "green"
        else:
            indicator = None
        tyre = self._tyre.get(num) or {}
        payload = {
            "race": True,
            "position": self._r_pos.get(num),
            "status": status,
            "intText": self._int_text.get(num),
            "intMs": int_ms,
            "intColour": self._int_colour.get(num) or None,
            "indicator": indicator,
            "tyreCompound": tyre.get("compound"),
            "tyreNew": tyre.get("isNew"),
            "tyreAge": tyre.get("age"),
        }
        if payload != self._last.get(num):
            self._last[num] = dict(payload)
            self._bus.emit(f"dashInfo:{num}", payload, clock_time)

    # ── inputs ────────────────────────────────────────────────────────────
    def _wild(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("lapPrediction:"):
            self._on_pred(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLaps:"):
            self._on_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverBestLapColour:"):
            self._on_best_colour(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLapClassification:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict) and data.get("type") is not None:
                self._cls[num] = data["type"]
                self._emit(num, clock_time)
        elif topic.startswith("driverStatus:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, str):
                self._status[num] = data
                self._emit(num, clock_time)

    def _on_pred(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        status = data.get("status")
        proj = data.get("projectedMs")
        colour = data.get("posColour") or ""
        pp = data.get("predictedPos")
        if status == "lapEnd":
            # OBSERVED: hold the last predicted values; actual time/position replace them on the
            # next driverLaps / standings.
            self._mode[num] = "observed"
            if proj is not None:
                self._lap_time[num] = {"ms": proj, "dp": 1, "colour": colour}
                self._position[num] = {"text": f"P{pp}", "colour": colour} if pp is not None else None
        elif pp is not None:
            # PREDICTING: projected lap time (ms=None → the client shows "-:--.-" when there's no
            # reference yet) + predicted position.
            self._mode[num] = "predicting"
            self._lap_time[num] = {"ms": proj, "dp": 1, "colour": colour}
            self._position[num] = {"text": f"P{pp}", "colour": colour}
        else:
            # blank prediction (out lap / aborted) → clear the time + position rows.
            self._mode[num] = None
            self._lap_time[num] = None
            self._position[num] = None
        self._emit(num, clock_time)

    def _on_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        ll = data.get("lastLap")
        if not isinstance(ll, dict) or ll.get("lap") is None or not ll.get("time"):
            return
        ms = _lap_ms(ll["time"])
        if ms is None:
            return
        # Only take the lap time while OBSERVED (right after a lapEnd) — the next driverLaps message.
        # Colour = best-lap band when it's a personal best (purple if overall fastest), WHITE when
        # the lap is slower than the driver's personal best. The position shares that colour. (user)
        if self._mode.get(num) == "observed":
            pb = bool(ll.get("personalBest") or ll.get("overallBest"))
            colour = ("purple" if ll.get("overallBest")
                      else (self._best_colour.get(num) or "green")) if pb else "white"
            self._obs_colour[num] = colour
            self._obs_pb[num] = pb
            self._lap_time[num] = {"ms": ms, "dp": 3, "colour": colour}
            if self._position.get(num):
                self._position[num] = {"text": self._position[num]["text"], "colour": colour}
            self._emit(num, clock_time)

    def _on_best_colour(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict) or not data.get("colour"):
            return
        self._best_colour[num] = data["colour"]
        # If the observed lap was a PB, its band may finalize after driverLaps — re-tint in place.
        if self._mode.get(num) == "observed" and self._obs_pb.get(num):
            self._obs_colour[num] = data["colour"]
            if self._lap_time.get(num):
                self._lap_time[num] = {**self._lap_time[num], "colour": data["colour"]}
            if self._position.get(num):
                self._position[num] = {**self._position[num], "colour": data["colour"]}
            self._emit(num, clock_time)

    def _on_standings(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for d in data.get("drivers", []):
            if isinstance(d, dict) and "num" in d and "position" in d:
                num = str(d["num"])
                self._pos[num] = d["position"]
                if self._mode.get(num) == "observed":       # keep updating the actual position number
                    self._position[num] = {"text": f"P{d['position']}",
                                           "colour": self._obs_colour.get(num, "")}
                    self._emit(num, clock_time)

    # ── output ────────────────────────────────────────────────────────────
    def _emit(self, num: str, clock_time: datetime) -> None:
        status = self._status.get(num)
        running = status not in ("PIT", "STOP", "RET")     # stopwatch: current lap unless in the garage
        mode = self._mode.get(num)
        payload = {
            "running": running,
            "lapTimeLabel": ("FORECAST" if mode == "predicting"
                             else "LAP TIME" if mode == "observed" else None),
            "lapTime": self._lap_time.get(num),
            "position": self._position.get(num),
            "flag": _flag_colour(status, self._cls.get(num, "")),
        }
        if payload != self._last.get(num):
            self._last[num] = dict(payload)
            self._bus.emit(f"dashInfo:{num}", payload, clock_time)


def _flag_colour(status: Optional[str], cls: str):
    if status == "PIT" or cls == "PIT":
        return "orange"
    if status == "STOP" or cls == "STOP":
        return "red"
    if cls == "PUSH":
        return "green"
    if cls in ("SLOW", "OUT"):
        return "white"
    return None
