"""Data Health Processor (card 20) — monitors the three streams that matter for
data quality and emits a `dataHealth` status for the client status footer.

Three independent streams are assessed, each over the drivers currently ON TRACK
(RET / STOP / PIT / FINISHED / DSQ cars are excluded — a retired or parked car
legitimately stops sending position/telemetry and must not count):

  - TIMING    — a driver's TimingData hasn't updated within the threshold.
  - TELEMETRY — CarData invalid (throttle/brake > 100, or speed 0 while the car
                is being position-tracked) OR missing (no recent CarData).
  - POSITION  — a driver's Position hasn't updated within the threshold.

Each stream's level is set by the FRACTION of on-track drivers affected:
  >50% → red,  >25–50% → orange,  >0–25% → yellow,  none → green.

Staleness is judged in DATA-clock time and only under GREEN (red/SC/VSC pause the
data legitimately). Evaluation is triggered by Heartbeat (keeps ticking when
TimingData stalls) and TimingData. `dataHealth` is emitted only on change; the
client restores the latest on connect/seek (latest-per-topic).

Emits: dataHealth {
    timing/telemetry/position: { level, drivers: [TLA…] }, green, onTrack }
"""

import json
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

ON_TRACK = {"TRACK", "OUT"}   # everything else (PIT/STOP/RET/FINISHED/DSQ/…) excluded


def _ms(dt: Optional[datetime]) -> Optional[float]:
    return dt.timestamp() * 1000 if dt else None


class DataHealthProcessor(Processor):
    TIMING_STALE_MS = 10000     # per-driver timing — a few seconds + tolerance
    POS_STALE_MS = 8000
    CARDATA_STALE_MS = 8000
    MOVE_RECENT_MS = 3000       # position-tracked recency for the speed=0 check
    GREEN_GRACE_MS = 15000      # after green resumes, let all streams catch up first

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._green = False
        self._tla: dict[str, str] = {}
        self._dstatus: dict[str, str] = {}
        self._last_timing: dict[str, float] = {}
        self._last_pos: dict[str, float] = {}
        self._last_cardata: dict[str, float] = {}
        self._invalid: dict[str, float] = {}     # num -> data-ms while last sample invalid
        self._green_since: Optional[float] = None  # data-ms green last resumed (staleness floor)
        self._last_emitted: Optional[str] = None

    def subscribe(self) -> None:
        self._bus.on("driverList", self._on_driver_list)
        self._bus.on("trackStatus", self._on_track_status)
        self._bus.on("TimingData", self._on_timing)
        self._bus.on("Position.z", self._on_position)
        self._bus.on("CarData.z", self._on_cardata)
        self._bus.on("Heartbeat", self._on_heartbeat)
        self._bus.on("*", self._on_any)          # driverStatus:{num} (no prefix subs server-side)

    def _on_any(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverStatus:") and isinstance(data, str):
            self._dstatus[topic.split(":", 1)[1]] = data

    def _on_driver_list(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            for num, d in data.items():
                if isinstance(d, dict) and d.get("tla"):
                    self._tla[str(num)] = d["tla"]

    def _on_track_status(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            was = self._green
            self._green = data.get("status") == "green"
            if self._green and not was:
                # Green resumed — anchor the staleness floor here so the streams
                # get a threshold's grace to catch up (no false stale at restart).
                self._green_since = _ms(clock_time)

    def _on_timing(self, data: Any, clock_time: datetime) -> None:
        t = _ms(clock_time)
        if isinstance(data, dict) and isinstance(data.get("Lines"), dict) and t is not None:
            for num in data["Lines"]:
                self._last_timing[str(num)] = t
        self._evaluate(clock_time)

    def _on_position(self, data: Any, clock_time: datetime) -> None:
        t = _ms(clock_time)
        if t is None:
            return
        snaps = data.get("Position") if isinstance(data, dict) else None
        latest = snaps[-1] if isinstance(snaps, list) and snaps else (data if isinstance(data, dict) else {})
        entries = latest.get("Entries") if isinstance(latest, dict) else None
        if isinstance(entries, dict):
            for num in entries:
                self._last_pos[str(num)] = t

    def _on_cardata(self, data: Any, clock_time: datetime) -> None:
        t = _ms(clock_time)
        if not isinstance(data, dict) or t is None:
            return
        entries = data.get("Entries")
        if not isinstance(entries, list):
            return
        for entry in entries:
            cars = entry.get("Cars") if isinstance(entry, dict) else None
            if not isinstance(cars, dict):
                continue
            for num, car in cars.items():
                if not isinstance(car, dict):
                    continue
                num = str(num)
                self._last_cardata[num] = t
                ch = car.get("Channels")
                if not isinstance(ch, dict):
                    continue
                speed, thr, brk = ch.get("2", 0), ch.get("4", 0), ch.get("5", 0)
                lp = self._last_pos.get(num)
                tracked = lp is not None and (t - lp) < self.MOVE_RECENT_MS
                invalid = (
                    (isinstance(thr, (int, float)) and thr > 100)
                    or (isinstance(brk, (int, float)) and brk > 100)
                    or (speed == 0 and tracked)
                )
                if invalid:
                    self._invalid[num] = t
                else:
                    self._invalid.pop(num, None)

    def _on_heartbeat(self, data: Any, clock_time: datetime) -> None:
        self._evaluate(clock_time)

    def _tlas(self, nums) -> list:
        return sorted(self._tla.get(n, n) for n in nums)

    @staticmethod
    def _level(bad: int, total: int) -> str:
        if total == 0 or bad == 0:
            return "green"
        pct = 100.0 * bad / total
        if pct > 50:
            return "red"
        if pct > 25:
            return "orange"
        return "yellow"

    def _evaluate(self, clock_time: datetime) -> None:
        now = _ms(clock_time)
        if now is None:
            return
        on_track = [n for n, s in self._dstatus.items() if s in ON_TRACK]
        total = len(on_track)

        # Brief grace right after green resumes so the streams can catch up
        # (cars cycle through a fresh update within ~10 s) — avoids the restart
        # transient after a red flag / SC. After the grace, absolute staleness.
        in_grace = self._green_since is not None and (now - self._green_since) < self.GREEN_GRACE_MS
        if not self._green or total == 0 or in_grace:
            timing_bad, pos_bad, tel_bad = [], [], []
        else:
            def stale(last, thr):
                return last is None or (now - last) > thr

            timing_bad = [n for n in on_track if stale(self._last_timing.get(n), self.TIMING_STALE_MS)]
            pos_bad = [n for n in on_track if stale(self._last_pos.get(n), self.POS_STALE_MS)]
            tel_bad = [n for n in on_track
                       if n in self._invalid or stale(self._last_cardata.get(n), self.CARDATA_STALE_MS)]

        payload = {
            "timing": {"level": self._level(len(timing_bad), total), "drivers": self._tlas(timing_bad)},
            "telemetry": {"level": self._level(len(tel_bad), total), "drivers": self._tlas(tel_bad)},
            "position": {"level": self._level(len(pos_bad), total), "drivers": self._tlas(pos_bad)},
            "green": self._green,
            "onTrack": total,
        }
        key = json.dumps(payload, sort_keys=True)
        if key != self._last_emitted:
            self._last_emitted = key
            self._bus.emit("dataHealth", payload, clock_time)
