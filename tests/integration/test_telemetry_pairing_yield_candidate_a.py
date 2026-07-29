"""AC-1's stated pass bar (requirement-spec.md AC-1: "pairing yield recovers
to within ~0.1pp of the oracle, and within-.z disorder drops to <=0.2%"),
end-to-end through `StreamNormalizer` -> `TelemetryProcessor`
(file-impact-map.md §7's own recommendation: "add a NEW integration-level
test that pipes the same batched-arrival fixture shape through
StreamNormalizer -> TelemetryProcessor end-to-end").

`tests/regression/test_telemetry_pairing_yield_d7b.py`'s existing red gate
feeds TelemetryProcessor directly, bypassing StreamNormalizer entirely — it
cannot detect Candidate A's fix (its own docstring says so) because the fix
lives one layer upstream, in `StreamNormalizer._process_z`'s buffering. This
file closes that gap: real compressed `.z` RawLines -> `StreamNormalizer.
process_line()` -> the resulting `NormalizedMessage`s fed to
`TelemetryProcessor._handle_car_data`/`_handle_position` in RELEASE order
(the order StreamNormalizer actually emits them, not file order) — exactly
the chain a `.z` sample travels through in production (Position.z's "position"
bus hop through PositionProcessor is elided; PositionProcessor does not
reorder or delay anything, it emits synchronously per received entry with the
entry's own clock_time, so this substitution preserves timing fidelity for
what's under test here — the ORDER/TIMING of StreamNormalizer's `.z` release,
not PositionProcessor's own projection math).

NO IMPLEMENTATION EXISTS YET. This is a feature test derived from the
acceptance criterion.
"""
import base64
import json
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.processing.file_reader import RawLine
from app.processing.stream_normalizer import StreamNormalizer
from app.processing.processors.telemetry_processor import TelemetryProcessor

NUM = "1"
BATCH_SIZE = 4
NUM_ROUNDS = 12
ROUND_SPAN_S = 1.0


def _compress(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = co.compress(raw) + co.flush()
    return base64.b64encode(compressed).decode("ascii")


def _car_data_z_envelope(entries: list[tuple[str, int]]) -> str:
    """entries: list of (iso_ts, speed)."""
    return _compress({"Entries": [
        {"Utc": ts, "Cars": {NUM: {"Channels": {"2": speed}}}} for ts, speed in entries
    ]})


def _position_z_envelope(entries: list[tuple[str, float]]) -> str:
    """entries: list of (iso_ts, dp)."""
    return _compress({"Position": [
        {"Timestamp": ts, "Entries": {NUM: [0, 0, dp]}} for ts, dp in entries
    ]})


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _build_batched_fixture() -> list[RawLine]:
    """Realistic CarData.z/Position.z batching: per round, F1 sends a
    CarData.z envelope covering the round's time window, THEN (in FILE
    order) a Position.z envelope covering the SAME window — architecture-
    plan.md §A.7.1's finding: "the Position entries covering a CarData
    batch's time window are frequently still in the next envelope when that
    batch is processed." dp advances monotonically (never wraps) so this
    fixture is purely about pairing yield, not lap/S-F logic (same design
    choice as tests/regression/test_telemetry_pairing_yield_d7b.py)."""
    base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
    lines: list[RawLine] = []
    dp = 0.0
    for rnd in range(NUM_ROUNDS):
        round_start = base + timedelta(seconds=rnd * ROUND_SPAN_S)

        car_entries = []
        for i in range(BATCH_SIZE):
            ts = round_start + timedelta(milliseconds=100 + i * 250)
            car_entries.append((_iso(ts), 200 + i))
        car_envelope_ts = round_start + timedelta(milliseconds=100 + (BATCH_SIZE - 1) * 250 + 50)
        lines.append(RawLine(topic="CarData.z", data=_car_data_z_envelope(car_entries),
                              envelope_ts=car_envelope_ts))

        pos_entries = []
        for i in range(BATCH_SIZE):
            ts = round_start + timedelta(milliseconds=i * 250)
            dp += 0.5
            pos_entries.append((_iso(ts), dp))
        pos_envelope_ts = round_start + timedelta(milliseconds=(BATCH_SIZE - 1) * 250 + 50)
        lines.append(RawLine(topic="Position.z", data=_position_z_envelope(pos_entries),
                              envelope_ts=pos_envelope_ts))

    return lines


def _run_through_normalizer_and_telemetry(lines: list[RawLine]):
    norm = StreamNormalizer()
    bus = mock.Mock()
    bus.on = mock.Mock()
    bus.emit = mock.Mock()
    proc = TelemetryProcessor(bus, "practice")
    drv = proc._drv(NUM)
    drv.activated = True

    all_msgs = []
    for line in lines:
        all_msgs.extend(norm.process_line(line))

    car_entries_fed = 0
    for msg in all_msgs:
        if msg.topic == "Position.z":
            entries = msg.data.get("Position", [])
            for e in entries:
                for n, coords in e.get("Entries", {}).items():
                    proc._handle_position({n: coords}, msg.utc_timestamp)
        elif msg.topic == "CarData.z":
            proc._handle_car_data(msg.data, msg.utc_timestamp)
            car_entries_fed += 1
    return proc, drv, car_entries_fed


class PairingYieldRecoversUnderCandidateA(unittest.TestCase):
    def test_yield_recovers_close_to_oracle_end_to_end_RED_GATE(self):
        lines = _build_batched_fixture()
        proc, drv, car_entries_fed = _run_through_normalizer_and_telemetry(lines)

        paired = sum(1 for s in drv.samples if s[0] is not None)
        yield_ratio = paired / car_entries_fed if car_entries_fed else 0.0

        self.assertGreaterEqual(
            yield_ratio, 0.95,
            f"end-to-end pairing yield through StreamNormalizer -> "
            f"TelemetryProcessor is only {yield_ratio:.1%} of {car_entries_fed} "
            f"CarData entries fed — AC-1's reorder buffer (scoped to "
            f"CarData.z/Position.z in stream_normalizer.py) does not exist "
            f"yet, so StreamNormalizer still releases .z entries in strict "
            f"file/arrival order and the batched-envelope pairing collapse "
            f"(architecture-plan.md §A.7.1) reproduces at this integration "
            f"level too",
        )


class WithinZDisorderDropsToNearZero(unittest.TestCase):
    """AC-1: "disorder drops from 37-45% today to 0.003-0.153% under
    Candidate A." Operationalized here as the fraction of adjacent RELEASED
    .z messages (both topics combined, in the order StreamNormalizer emits
    them) whose timestamp is a step BACKWARD relative to the previous
    release — an inversion. A correctly buffered/reordered release stream is
    monotonic non-decreasing across topics; today's unbuffered pass-through
    is not, because this fixture deliberately interleaves CarData/Position
    envelopes with lower timestamps arriving later in file order."""

    def test_cross_topic_release_order_is_near_monotonic_RED_GATE(self):
        lines = _build_batched_fixture()
        norm = StreamNormalizer()
        all_msgs = []
        for line in lines:
            all_msgs.extend(norm.process_line(line))
        z_msgs = [m for m in all_msgs if m.topic in ("CarData.z", "Position.z")]

        inversions = 0
        prev_ts = None
        for m in z_msgs:
            if prev_ts is not None and m.utc_timestamp < prev_ts:
                inversions += 1
            prev_ts = m.utc_timestamp
        disorder_ratio = inversions / len(z_msgs) if z_msgs else 0.0

        self.assertLessEqual(
            disorder_ratio, 0.002,
            f"cross-topic release-order disorder is {disorder_ratio:.1%} "
            f"({inversions}/{len(z_msgs)} adjacent releases go backward in "
            f"time) — AC-1's buffer, which would restore near-monotonic "
            f"timestamp-ordered release, does not exist yet",
        )


if __name__ == "__main__":
    unittest.main()
