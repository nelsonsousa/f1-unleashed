"""AC-14 (`requirement-spec.md` §8.1, WB3, `docs/artifacts/2026-08-01-040-merged-position-
telemetry-processor/`): `TelemetryProcessor._emit_lap`'s DTW lap-commit re-derivation gate,
rewritten from the old per-sample boolean `est` majority test to a majority test on
`msSinceLastKnown` (`sample[7]`) exceeding `EST_THRESHOLD_S * 1000` ms.

Written from `requirement-spec.md` AC-14 and a direct read of the shipped gate
(`telemetry_processor.py`, `_emit_lap`, the `if self._matcher == "b" and self._prof_s:` block)
-- not from the implementer's own summary. Drives `_emit_lap` directly against hand-built
`drv.samples` (rather than through `_handle_car_data`) so the exact boundary value of
`sample[7]` on each sample is pinned down precisely, independent of whatever the reckoner
would produce for a given scenario -- this is a test of the GATE's arithmetic, not of how
`msSinceLastKnown` gets computed (that's `test_telemetry_processor_wb3_dp_reckoning.py`'s job).

`_dtw_relabel_dp` itself (the actual DTW alignment) is mocked out here -- this suite verifies
WHETHER it fires, not what it produces, which is unchanged, pre-existing behavior this task
does not touch.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.processors.telemetry_processor import TelemetryProcessor, EST_THRESHOLD_S, _epoch_ms

NUM = "44"
THRESHOLD_MS = EST_THRESHOLD_S * 1000.0


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _proc() -> TelemetryProcessor:
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    proc = TelemetryProcessor(bus, "race")
    proc._matcher = "b"          # DTW-selected circuit
    proc._prof_s = [100.0, 150.0, 120.0]   # truthy, non-empty speed profile
    proc._prof_dp = [0.0, 50.0, 100.0]
    return proc


def _sample(offset_s: float, ms_since_last_known) -> list:
    """[dp, speed, rpm, gear, thr, brk, abs_ms, msSinceLastKnown] -- abs_ms is
    a REAL epoch-ms value (matching `_emit_lap`'s own `_epoch_ms(start_ts)`/
    `_epoch_ms(end_ts)` window comparison -- a small synthetic offset like
    `t_ms` alone would silently fall outside any real start/end window and
    make `in_lap` empty regardless of what this test intends). speed nonzero
    so the (race, so unfiltered anyway) row survives untouched."""
    return [offset_s * 10.0, 250, 10000, 4, 80, 0, _epoch_ms(_t(offset_s)), ms_since_last_known]


def _build_lap(proc: TelemetryProcessor, drv_num: str, staleness_values: list) -> "DriverData":
    drv = proc._drv(drv_num)
    drv.activated = True
    drv.samples = [_sample(i * 0.5, sv) for i, sv in enumerate(staleness_values)]
    return drv


class MajorityStaleFiresTheGate(unittest.TestCase):
    def test_majority_of_samples_stale_triggers_dtw_relabel(self):
        proc = _proc()
        # 3 of 5 samples well over the threshold -> majority stale.
        staleness = [1500.0, 1500.0, 1500.0, 200.0, 200.0]
        drv = _build_lap(proc, NUM, staleness)
        n_ms = len(drv.samples) * 500

        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(n_ms / 1000.0 + 1.0))

        m_dtw.assert_called_once()

    def test_all_samples_stale_triggers_dtw_relabel(self):
        proc = _proc()
        staleness = [5000.0] * 6
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_called_once()


class MajorityFreshDoesNotFireTheGate(unittest.TestCase):
    def test_majority_of_samples_fresh_does_not_trigger_dtw_relabel(self):
        proc = _proc()
        # 3 of 5 samples well under the threshold -> majority fresh.
        staleness = [200.0, 200.0, 200.0, 1500.0, 1500.0]
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()

    def test_all_samples_fresh_does_not_trigger_dtw_relabel(self):
        proc = _proc()
        staleness = [50.0] * 6
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()

    def test_a_healthy_lap_with_universal_sub_500ms_phase_drift_gaps_never_fires(self):
        """The realistic healthy-feed shape §8.1 grounds the threshold choice
        on: a whole lap of samples sitting well under 1000ms (the phase-drift
        gap AC-2 targets is always <=~500ms) must never trip the gate."""
        proc = _proc()
        staleness = [round(i * 37.5, 1) % 480.0 for i in range(40)]  # all << 1000ms
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(30.0))
        m_dtw.assert_not_called()


class BoundaryAtEstThresholdMs(unittest.TestCase):
    """The comparison is strict `>` (`s[7] > EST_THRESHOLD_S * 1000`) -- a
    value of EXACTLY the threshold does not count as stale."""

    def test_value_exactly_at_threshold_does_not_count_as_stale(self):
        proc = _proc()
        # All samples sit exactly AT the threshold -- none should count as
        # stale, so 0 stale of N is never a majority.
        staleness = [THRESHOLD_MS] * 5
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()

    def test_value_just_over_threshold_counts_as_stale_and_can_tip_the_majority(self):
        proc = _proc()
        # 3 of 5 just barely over the threshold -> majority stale.
        staleness = [THRESHOLD_MS + 0.1, THRESHOLD_MS + 0.1, THRESHOLD_MS + 0.1,
                     THRESHOLD_MS - 0.1, THRESHOLD_MS - 0.1]
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_called_once()

    def test_value_just_under_threshold_counts_as_fresh(self):
        proc = _proc()
        staleness = [THRESHOLD_MS - 0.1] * 5
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()

    def test_exact_tie_five_of_ten_stale_does_not_trigger(self):
        """The gate's own condition is `stale_n * 2 > len(in_lap)` (strict) --
        an exact 50/50 split must NOT trigger (5*2 == 10, not > 10)."""
        proc = _proc()
        staleness = [THRESHOLD_MS + 100.0] * 5 + [50.0] * 5
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()

    def test_one_more_than_half_stale_does_trigger(self):
        proc = _proc()
        staleness = [THRESHOLD_MS + 100.0] * 6 + [50.0] * 5
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_called_once()


class NoneStalenessValuesNeverCountAsStale(unittest.TestCase):
    """§6.4/§5: the residual pre-calibration/never-seeded window produces
    `msSinceLastKnown = None` -- must never count toward `stale_n`, even
    though a naive `s[7] > THRESHOLD` on `None` would raise a TypeError in
    Python 3 (proving the `s[7] is not None` guard is load-bearing, not
    decorative)."""

    def test_all_none_staleness_values_never_trigger_and_never_raise(self):
        proc = _proc()
        staleness = [None] * 6
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))   # must not raise TypeError
        m_dtw.assert_not_called()

    def test_mixed_none_and_stale_values_only_counts_the_non_none_stale_ones(self):
        proc = _proc()
        # 4 stale, 1 fresh, 5 None -- of the 10 total samples, only 4 count
        # as stale (4*2 == 8, not > 10) -> must NOT trigger.
        staleness = [THRESHOLD_MS + 100.0] * 4 + [50.0] + [None] * 5
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()


class MatcherAOrNoSignatureNeverFiresRegardlessOfStaleness(unittest.TestCase):
    """Non-regression: circuits without matcher "b" (or no profile loaded)
    must never run the DTW relabel, no matter how stale the lap's samples
    are -- this behavior predates WB3 and must be unchanged."""

    def test_matcher_a_never_fires_even_with_all_samples_stale(self):
        proc = _proc()
        proc._matcher = "a"
        staleness = [5000.0] * 6
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()

    def test_no_profile_loaded_never_fires_even_with_matcher_b(self):
        proc = _proc()
        proc._prof_s = None
        staleness = [5000.0] * 6
        drv = _build_lap(proc, NUM, staleness)
        with mock.patch.object(proc, "_dtw_relabel_dp") as m_dtw:
            proc._emit_lap(drv, 1, _t(0.0), _t(10.0))
        m_dtw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
