"""Data Health Processor (card 20) — monitors the critical raw data streams and
emits a `dataHealth` status for the client status footer.

Only four streams need active monitoring (SME-directed); all others are secondary
and ignored:
  1. Position stale  — a driver's TIMING updated recently but their Position did not.
  2. CarData invalid — telemetry out of range: throttle/brake > 100, or speed 0
     while the car is moving (mirrors telemetry_processor's validity rule).
  3. CarData missing — timing updated recently but no CarData for that driver.
  4. TimingData stale — the crucial stream; under GREEN it must tick every few
     seconds, else everything downstream fails.

Staleness is judged in DATA-clock time and ONLY under GREEN — red/SC/VSC pause the
data legitimately (e.g. the Monaco red-flag stoppage). Evaluation is triggered by
Heartbeat (F1 keepalive — keeps ticking even when TimingData stalls) and by
TimingData itself. `dataHealth` is emitted only when the status changes; the client
restores the latest on connect/seek (latest-per-topic).

Emits: dataHealth { status, green, timing, positionStale[], carDataMissing[],
                    carDataInvalid[] }   (driver lists are TLAs)
"""

import json
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _ms(dt: Optional[datetime]) -> Optional[float]:
    return dt.timestamp() * 1000 if dt else None


class DataHealthProcessor(Processor):
    # Thresholds, in DATA-clock ms.
    TIMING_STALE_MS = 5000     # crucial stream — a few seconds under green
    POS_STALE_MS = 8000        # position behind a fresh timing update
    CARDATA_STALE_MS = 8000    # carData behind a fresh timing update
    TIMING_FRESH_MS = 6000     # "this driver's timing updated recently" window

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._green = False
        self._tla: dict[str, str] = {}
        self._last_timing: dict[str, float] = {}
        self._last_pos: dict[str, float] = {}
        self._last_cardata: dict[str, float] = {}
        self._invalid: dict[str, float] = {}     # num -> data-ms of last invalid sample
        self._last_timing_any: Optional[float] = None
        self._last_emitted: Optional[str] = None

    def subscribe(self) -> None:
        self._bus.on("driverList", self._on_driver_list)
        self._bus.on("trackStatus", self._on_track_status)
        self._bus.on("TimingData", self._on_timing)
        self._bus.on("Position.z", self._on_position)
        self._bus.on("CarData.z", self._on_cardata)
        self._bus.on("Heartbeat", self._on_heartbeat)

    def _on_driver_list(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            for num, d in data.items():
                if isinstance(d, dict) and d.get("tla"):
                    self._tla[str(num)] = d["tla"]

    def _on_track_status(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            self._green = data.get("status") == "green"

    def _on_timing(self, data: Any, clock_time: datetime) -> None:
        t = _ms(clock_time)
        if isinstance(data, dict) and isinstance(data.get("Lines"), dict) and t is not None:
            self._last_timing_any = t
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
                if isinstance(ch, dict):
                    speed, thr, brk = ch.get("2", 0), ch.get("4", 0), ch.get("5", 0)
                    invalid = (
                        (isinstance(thr, (int, float)) and thr > 100)
                        or (isinstance(brk, (int, float)) and brk > 100)
                        or speed == 0
                    )
                    if invalid:
                        self._invalid[num] = t
                    else:
                        self._invalid.pop(num, None)

    def _on_heartbeat(self, data: Any, clock_time: datetime) -> None:
        self._evaluate(clock_time)

    def _tlas(self, nums) -> list:
        return sorted(self._tla.get(n, n) for n in nums)

    def _evaluate(self, clock_time: datetime) -> None:
        now = _ms(clock_time)
        if now is None:
            return

        # 4. TimingData stale (crucial) — green only.
        timing_stale = (
            self._green and self._last_timing_any is not None
            and (now - self._last_timing_any) > self.TIMING_STALE_MS
        )

        pos_stale: list = []
        cd_missing: list = []
        cd_invalid: list = []
        if self._green:
            for num, tt in self._last_timing.items():
                # Only judge a driver whose timing is currently fresh (1 & 3).
                if (now - tt) > self.TIMING_FRESH_MS:
                    continue
                lp = self._last_pos.get(num)
                if lp is None or (now - lp) > self.POS_STALE_MS:
                    pos_stale.append(num)
                lc = self._last_cardata.get(num)
                if lc is None or (now - lc) > self.CARDATA_STALE_MS:
                    cd_missing.append(num)
            for num, it in self._invalid.items():     # 2
                if (now - it) <= self.TIMING_FRESH_MS:
                    cd_invalid.append(num)

        status = (
            "critical" if timing_stale
            else "warn" if (pos_stale or cd_missing or cd_invalid)
            else "ok"
        )
        payload = {
            "status": status,
            "green": self._green,
            "timing": "stale" if timing_stale else "ok",
            "positionStale": self._tlas(pos_stale),
            "carDataMissing": self._tlas(cd_missing),
            "carDataInvalid": self._tlas(cd_invalid),
        }
        key = json.dumps(payload, sort_keys=True)
        if key != self._last_emitted:
            self._last_emitted = key
            self._bus.emit("dataHealth", payload, clock_time)
