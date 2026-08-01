"""
DpReckoner — shared dead-reckoning distance-percentage (dp%) estimator.

Extracted from `PositionProcessor` (WB1,
docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/) so both
`PositionProcessor` and, later, `TelemetryProcessor` (WB2/WB3) can share ONE
calibrated speed->distance conversion factor and ONE per-car reckoning
state instead of two independently-derived, independently-drifting copies
-- the "single reckoner, not two" hard requirement in `requirement-spec.md`
§1a/§4.

This module is a plain calculation/state helper, not a `Processor` -- it
does not subscribe to `SessionMessageBus` itself. Whatever processor(s) end
up calling it decide when and how often to do so.

Two operations:

- `observe_real_position(num, dp, clock_time)` -- feed a REAL, measured
  position fix. Refines the calibrated conversion factor `C` (dp% per
  km/h*second) and anchors the car's reckoning state to this known-true dp,
  discarding any drift accumulated since the last real fix.
- `advance(num, speed, clock_time) -> AdvanceResult` -- dead-reckon the
  car's dp forward from its last anchor/advance by the elapsed time to
  `clock_time`, at `speed` (km/h), using the calibrated factor. Returns
  `dp=None` until a car has been seeded by a real fix, or until the factor
  itself is calibrated (see `MIN_CAL_N`) -- there is nothing to reckon FROM
  yet in either case.

AC-7 (the bug this module exists to fix): `advance()` integrates the FULL
elapsed `dt` every time, never silently discarding time beyond a fixed
ceiling the way `position_processor.py` did pre-merge
(`dt = min(dt, MAX_DT_S)`, MAX_DT_S=2.0s -- any CarData stall longer than
2.0s had the excess silently dropped from the integration, with nothing
downstream able to tell it happened). `SAFE_MAX_DT_S` below is NOT that
clamp reincarnated: it is a guard against genuinely pathological input (a
corrupted or out-of-order timestamp implying an hours-long "stall"), sits
three orders of magnitude above any real feed stall this project has
observed, and is not reachable by AC-7's own 5-second red-gate case
(`tests/regression/test_position_processor_max_dt_stall_discard_ac7.py`).

Ordering-hazard note (file-impact-map.md §1.3, milestone-plan.md WB1 "The
message_bus dispatch-ordering hazard is resolved inside WB1"):
`SessionMessageBus` dispatches same-topic handlers in registration order,
and `preprocessor.py` registers `PositionProcessor` before
`TelemetryProcessor` -- today harmless because only `PositionProcessor`
touches reckoning state on `CarData.z`, but load-bearing the moment a
second processor also calls into a SHARED reckoner for the same tick.
`advance()` resolves this by construction, not by comment: it updates the
car's last-integration timestamp to `clock_time` unconditionally, before
checking whether any time has actually elapsed. A second call for the same
car at the same `clock_time` therefore always computes `dt == 0` and is a
safe, deterministic no-op -- never a double-integration -- regardless of
which caller (or how many callers) reach it first for that tick.

IMPORTANT for WB3 (flagged by change-reviewer, docs/artifacts/2026-08-01-040-*/review-findings.md):
"safe no-op" means the SECOND caller's `advance()` returns `dp=None` for
that tick (see `dt == 0` -> `AdvanceResult(dp=None, ...)` below) -- it does
NOT hand back the first caller's result. If WB3 wires TelemetryProcessor to
call `advance()` independently after PositionProcessor already has for the
same tick, TelemetryProcessor gets `dp=None` on every single CarData
message, unconditionally -- a silent, 100%-reproducible failure of AC-2,
not a race. What WB1 actually delivers is that a second call is SAFE
(no double-integration, no corrupted state) -- not that a second call is
USEFUL. WB3 needs an explicit way to retrieve "this tick's already-computed
result" (e.g. a shared call site, or a per-tick cache read) rather than
calling `advance()` a second time expecting a real value.

Determinism (AC-12): every method here is a pure function of its arguments
and prior state -- no wall-clock ("now") is read anywhere, only
`clock_time` (the message's own timestamp, passed in by the caller) -- and
all per-car state is `dict[str, ...]` keyed by car number, never iterated
in a way whose order affects the result.
"""

from datetime import datetime
from typing import NamedTuple, Optional
import logging

logger = logging.getLogger(__name__)

MIN_CAL_N = 30              # position intervals needed before C is trusted
CAL_DDP_MIN = 0.001          # sane forward-advance floor (rejects a stale-repeat real fix)
CAL_DDP_MAX = 10.0           # sane forward-advance ceiling (rejects a wrap/pit-lane jump)
CAL_CARDATA_DT_MAX_S = 5.0   # reject implausible CarData gaps from feeding the sv accumulator
CAL_LOG_AT_N = 400           # one-shot diagnostic log once calibration has plenty of samples

# AC-7: NOT the MAX_DT_S clamp this module replaces. A pathological-input
# guard only -- see the module docstring. Three orders of magnitude above
# any real CarData stall and above the AC-7 red-gate's own 5.0s case.
SAFE_MAX_DT_S = 3600.0


class AdvanceResult(NamedTuple):
    """Outcome of one `DpReckoner.advance()` call."""
    dp: Optional[float]     # new reckoned dp% (wrapped 0-100), or None if not advanceable this tick
    ddp: Optional[float]    # the raw (unwrapped) distance-% delta just integrated, or None
    dt: float                # elapsed seconds since this car's last advance/observation (>= 0)
    calibrated: bool         # whether C was known well enough (MIN_CAL_N) to integrate this tick


class DpReckoner:
    """One calibrated speed->distance conversion factor and one per-car
    dead-reckoning anchor, shared by any processor that needs a dp%
    estimate when no fresh real position fix is available."""

    def __init__(self) -> None:
        # Calibration (global across all cars -- one physical scale for the
        # whole session, learned from every car's motion).
        self._C: Optional[float] = None
        self._cal_dp_sum: float = 0.0                 # Σ real dp advanced (all cars)
        self._cal_sv_sum: float = 0.0                 # Σ speed·dt over CONTINUOUS CarData (all cars)
        self._cal_n: int = 0                          # position intervals contributing
        self._cal_prev_dp: dict[str, float] = {}      # per car: last real dp
        self._cal_car_ts: dict[str, datetime] = {}    # per car: last CarData time (sv dt)

        # Per-car reckoning anchor.
        self._dp: dict[str, float] = {}                # current reckoned dp%
        self._ts: dict[str, datetime] = {}              # last integration/observation time

    @property
    def C(self) -> Optional[float]:
        return self._C

    @property
    def calibrated(self) -> bool:
        return self._C is not None and self._cal_n >= MIN_CAL_N

    def is_seeded(self, num: str) -> bool:
        """Whether `num` has ever been anchored to a real position fix --
        the precondition for `advance()` to do anything meaningful for it."""
        return num in self._ts

    def current_dp(self, num: str) -> Optional[float]:
        """This car's current reckoned dp%, or None if never seeded."""
        return self._dp.get(num)

    def dp_snapshot(self) -> dict[str, float]:
        """A copy of every seeded car's current reckoned dp%, keyed by car
        number. A copy (not the live dict) so callers can iterate it while
        also calling back into this reckoner without mutating mid-iteration."""
        return dict(self._dp)

    def observe_real_position(self, num: str, dp: float, clock_time: datetime) -> None:
        """Feed a real, measured position fix for `num`. Refines the shared
        calibration factor `C` from the ratio of real distance advanced to
        speed*time elapsed, and resets this car's reckoning anchor to `dp`
        -- drift accumulated since the last real fix is discarded in favour
        of ground truth."""
        prev_dp = self._cal_prev_dp.get(num)
        if prev_dp is not None:
            ddp = (dp - prev_dp) % 100.0
            if CAL_DDP_MIN < ddp < CAL_DDP_MAX:        # sane forward advance, no wrap/pit jump
                self._cal_dp_sum += ddp
                self._cal_n += 1
                if self._cal_sv_sum > 0:
                    self._C = self._cal_dp_sum / self._cal_sv_sum
                    if self._cal_n == CAL_LOG_AT_N:
                        logger.info(
                            f"[recon] calibrated C={self._C:.5f} dp%/(kph·s) "
                            f"(~0.0083 expected for a 3.3 km lap)"
                        )
        self._cal_prev_dp[num] = dp
        self._dp[num] = dp
        self._ts[num] = clock_time

    def advance(self, num: str, speed: float, clock_time: datetime) -> AdvanceResult:
        """Dead-reckon `num`'s dp forward from its last anchor/advance by
        the elapsed time to `clock_time`, at `speed` (km/h). See the module
        docstring (AC-7) for the MAX_DT_S clamp this replaces, and for the
        idempotency property that resolves the CarData.z registration-order
        hazard by construction."""
        # Calibration denominator: Σ speed·dt over CONTINUOUS CarData steps
        # (not reset by real fixes -- covers the same span the position
        # deltas measure).
        prev_car_ts = self._cal_car_ts.get(num)
        self._cal_car_ts[num] = clock_time
        if prev_car_ts is not None:
            dtc = (clock_time - prev_car_ts).total_seconds()
            if 0 < dtc < CAL_CARDATA_DT_MAX_S:
                self._cal_sv_sum += speed * dtc

        last_ts = self._ts.get(num)
        if last_ts is None:
            # Never anchored to a real fix -- nothing to reckon from yet.
            return AdvanceResult(dp=None, ddp=None, dt=0.0, calibrated=self.calibrated)

        dt = (clock_time - last_ts).total_seconds()
        self._ts[num] = clock_time     # unconditional -- see the idempotency note above
        if dt <= 0:
            return AdvanceResult(dp=None, ddp=None, dt=max(dt, 0.0), calibrated=self.calibrated)

        dt = min(dt, SAFE_MAX_DT_S)     # pathological-input guard only, see module docstring
        if not self.calibrated:
            return AdvanceResult(dp=None, ddp=None, dt=dt, calibrated=False)

        ddp = self._C * speed * dt
        new_dp = (self._dp.get(num, 0.0) + ddp) % 100.0
        self._dp[num] = new_dp
        return AdvanceResult(dp=new_dp, ddp=ddp, dt=dt, calibrated=True)

    def set_dp(self, num: str, dp: float) -> None:
        """Override this car's current reckoned dp -- used by a caller
        applying reckoning logic this module doesn't own (e.g.
        `PositionProcessor`'s SC/VSC 99.9%-clamp, or its apex/ZigZag
        anchor-snap correction). Keeps subsequent `advance()` calls
        integrating forward from the corrected value instead of silently
        discarding the correction on the next tick."""
        self._dp[num] = dp % 100.0
