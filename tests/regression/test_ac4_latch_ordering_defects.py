"""AC-4 RED GATE (requirement-spec.md AC-4; file-impact-map.md §1 AC-4):
the two ordering-sensitive latches that read an UNBUFFERED topic's state and
apply it to `.z`-derived samples with no regard for the `.z` sample's OWN
payload timestamp:

  1. `position_processor.py::_sc_active` — set from `TrackStatus`, read in
     `_handle_car_data` at the moment a CarData/Position sample is processed.
  2. `telemetry_processor.py::drv.in_pit` / `_close_in_lap` — triggered from
     `driverStatus:{num}:PIT`, which snapshots `drv.samples` by timestamp
     window AT THE MOMENT it fires, via `_emit_lap`.

Both processors, as written today, have NO mechanism to defer a latch's
effect until the `.z` watermark has passed the latching message's own
timestamp — they simply act on whatever the current field value is at CALL
time, regardless of the payload timestamp carried by the `.z` sample being
processed. This is exactly what candidate A's 1.0s reorder buffer will make
observable in production (a `.z` entry logically PRECEDING a flip can be
released and processed AFTER the flip, once entries are buffered/reordered
by their own timestamp rather than delivered in strict arrival order) — but
the defect is a property of THESE TWO PROCESSORS' code as it exists right
now, and is reproducible today by directly driving them with calls in an
order that does not match the entries' own payload timestamps (exactly what
`test_telemetry_pairing_yield_d7b.py` already does to reach TelemetryProcessor
directly, bypassing StreamNormalizer entirely — the same white-box technique
is used here). No proposed fix has been read to write this test.

Both tests assert the CORRECT (deferred-by-own-timestamp) behavior and are
observed to FAIL against today's code — the standard red-gate shape.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np

from app.processing.processors.position_processor import PositionProcessor
from app.processing.processors.telemetry_processor import TelemetryProcessor
from app.processing.track_geometry import TrackGeometry

NUM = "44"


def _t(offset_s: float) -> datetime:
    return datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_s)


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _degenerate_geo(total_dist: float = 100.0) -> TrackGeometry:
    """A minimal single-segment "track" — just enough for `_dist_pct_to_xy`'s
    searchsorted/interpolation math to run without a real SVG."""
    return TrackGeometry(
        points=[(0.0, 0.0), (total_dist, 0.0)],
        seg_starts=np.array([[0.0, 0.0]]),
        seg_dirs=np.array([[total_dist, 0.0]]),
        seg_len_sq=np.array([total_dist ** 2]),
        seg_len=np.array([total_dist]),
        seg_cum_dist=np.array([0.0]),
        total_dist=total_dist,
        sf_offset=0.0,
        lap_distance=total_dist,
    )


class ScActiveAppliedToWrongTimedZEntry_RedGate(unittest.TestCase):
    """`position_processor._sc_active`: a CarData.z-driven dead-reckoning step
    whose OWN payload timestamp precedes the SC/VSC flag's own transition
    timestamp must still be reconstructed as if SC/VSC were NOT active (the
    flip hadn't logically happened yet, from that sample's point of view).
    Today the code has no such check — it just reads `self._sc_active`
    whatever it currently is, so calling `_handle_track_status` before
    `_handle_car_data` (regardless of the CarData entry's OWN earlier
    timestamp) wrongly applies the post-flip clamped/no-apex-snap branch.

    Observable divergence used: SC-active clamps `dp` at 99.9 and never sets
    `_wrapped[num]` on a would-be S/F wrap; the normal (non-SC) branch lets
    `dp` wrap naturally and sets `_wrapped[num] = True`. A correct
    (timestamp-deferred) implementation would treat this sample as
    pre-flip (SC not yet active) and allow the natural wrap.
    """

    def _proc(self) -> PositionProcessor:
        bus = mock.Mock()
        bus.on = mock.Mock()
        bus.emit = mock.Mock()
        proc = PositionProcessor(bus, "race")
        proc._geo = _degenerate_geo()
        # Minimal calibrated state so `_handle_car_data`'s dead-reckoning
        # branch actually runs (bypasses the real SVG/signature-loading and
        # multi-sample calibration process — direct white-box state seeding,
        # same technique test_telemetry_pairing_buffer.py already uses on
        # DriverData internals).
        proc._last_pos_ts[NUM] = _t(0)
        proc._r_ts[NUM] = _t(0)
        proc._r_dp[NUM] = 99.5   # one step from wrapping
        proc._C = 1.0
        proc._cal_n = 100
        proc._miss[NUM] = 0
        return proc

    def test_early_timestamped_z_entry_is_not_retroactively_sc_suppressed_RED_GATE(self):
        proc = self._proc()

        # The SC/VSC flag transitions at real time t=5s.
        proc._handle_track_status({"Status": "4"}, _t(5.0))
        self.assertTrue(proc._sc_active)

        # A `.z`-derived CarData sample whose OWN payload timestamp (t=1s) is
        # BEFORE the flip (t=5s) — i.e. this sample logically predates SC/VSC
        # — is only now being processed (buffer-held-then-released, or in
        # this direct unit test, simply called after `_handle_track_status`).
        car_data = {"Entries": [{"Cars": {NUM: {"Channels": {"2": 100}}}}]}
        proc._handle_car_data(car_data, _t(1.0))

        # Correct/fixed expectation: this sample predates the flip, so it
        # must be reconstructed as pre-SC (natural wrap allowed).
        self.assertTrue(
            proc._wrapped.get(NUM),
            "a .z sample whose own payload timestamp precedes the SC/VSC "
            "transition was wrongly SC-suppressed (clamped, no wrap) because "
            "position_processor._sc_active has no timestamp-based deferral",
        )


class InPitLatchDropsLateArrivingInWindowSample_RedGate(unittest.TestCase):
    """`telemetry_processor.py`: `driverStatus:{num}:PIT` closes the in-lap
    IMMEDIATELY via `_close_in_lap` -> `_emit_lap`, which snapshots
    `drv.samples` by timestamp window AT THAT MOMENT. A `.z` sample whose own
    payload timestamp falls INSIDE that already-closed window, but which is
    appended to `drv.samples` (i.e. actually processed/arrives) AFTER
    `_close_in_lap` ran, is silently and PERMANENTLY absent from the emitted
    lap — `_emit_lap` never re-runs for an already-`emitted` lap number.
    """

    def _proc(self) -> TelemetryProcessor:
        bus = mock.Mock()
        bus.on = mock.Mock()
        bus.emit = mock.Mock()
        return TelemetryProcessor(bus, "practice")

    def test_late_arriving_in_window_z_sample_is_captured_by_deferred_close_RED_GATE(self):
        """Repaired 2026-07-29 (fix-attempt 2, must-fix 2): the original
        version of this test asserted BOTH that PIT closes the lap
        IMMEDIATELY (`len(emitted_calls) == 1` checked right after the PIT
        call) AND that a late sample appended afterward retroactively shows
        up in that already-captured payload — mutually exclusive with
        genuine deferral (an immediately-emitted lap is, by construction, a
        snapshot nothing appended later can affect). The correct/fixed
        behavior is that PIT does NOT emit synchronously at all; the close
        is deferred until a REAL CarData.z/Position.z arrival's own
        timestamp catches up to the PIT message's own timestamp — this is
        the actual mechanism `file-impact-map.md` §1 / requirement-spec.md
        AC-4 require. The late sample is now routed through the real
        `_handle_position`/`_handle_car_data` handlers (matching the sibling
        `ScActiveAppliedToWrongTimedZEntry_RedGate` test's technique in this
        same file), not a raw `drv.samples.append()` list mutation, so the
        deferred trigger can actually fire."""
        proc = self._proc()
        drv = proc._drv(NUM)
        drv.activated = True
        start = _t(0.0)
        drv.crossings = [start]
        drv.cur_lap = 1

        # A sample already present before PIT arrives.
        drv.samples = [[10.0, 200, 10000, 3, 50, 0, _epoch_ms(_t(1.0)), 0]]

        # PIT arrives at t=5s. Correct/fixed behavior: this only LATCHES
        # in_pit and records the pending close timestamp — it must NOT emit
        # synchronously (that's what "genuine deferral" means).
        proc._handle_wildcard(f"driverStatus:{NUM}", "PIT", _t(5.0))
        emitted_calls = [c for c in proc._bus.emit.call_args_list
                          if c.args[0] == f"telemetryLap:{NUM}:1"]
        self.assertEqual(
            len(emitted_calls), 0,
            "PIT entry must defer the close (no synchronous emit) — a "
            "genuinely deferred design cannot also emit immediately",
        )

        # A genuinely late-arriving `.z` sample whose OWN payload timestamp
        # (t=3s) falls INSIDE the pending in-lap's window [0s, 5s) arrives
        # via the REAL handlers (buffer-held-then-released, or here, simply
        # processed after the PIT call) — not a raw list append.
        late_ts = _t(3.0)
        proc._handle_position({NUM: [0.0, 0.0, 50.0]}, late_ts)
        proc._handle_car_data(
            {"Entries": [{"Cars": {NUM: {
                "Channels": {"2": 210, "0": 11000, "3": 3, "4": 60, "5": 0}
            }}}]},
            late_ts,
        )

        # Still deferred: this sample's own timestamp (3s) is BEFORE the
        # PIT message's own timestamp (5s), so the close must not have fired
        # yet — proves the deferral is genuinely timestamp-gated, not just
        # "whatever arrives next".
        emitted_calls = [c for c in proc._bus.emit.call_args_list
                          if c.args[0] == f"telemetryLap:{NUM}:1"]
        self.assertEqual(
            len(emitted_calls), 0,
            "the close must still be deferred after an earlier-timestamped "
            ".z arrival — only a .z arrival whose OWN timestamp reaches the "
            "PIT message's own timestamp may release it",
        )

        # A subsequent `.z` arrival whose own timestamp reaches (>=) the PIT
        # message's own timestamp (5s) is what actually releases the
        # deferred close.
        proc._handle_position({NUM: [0.0, 0.0, 55.0]}, _t(5.0))

        emitted_calls = [c for c in proc._bus.emit.call_args_list
                          if c.args[0] == f"telemetryLap:{NUM}:1"]
        self.assertEqual(
            len(emitted_calls), 1,
            "the deferred close must fire once a .z arrival's own timestamp "
            "reaches the PIT message's own timestamp",
        )
        emitted_dp_values = [row[0] for row in emitted_calls[0].args[1]]
        self.assertIn(
            50.0, emitted_dp_values,
            "a .z sample whose own payload timestamp falls inside the "
            "pending in-lap's window, but which arrives (via the real "
            "handler) after PIT and before the deferred close fires, must "
            "be present in the emitted lap",
        )


if __name__ == "__main__":
    unittest.main()
