"""AC-1/AC-2/AC-3 — the scoped 1.0s reorder buffer in `StreamNormalizer._process_z`
(requirement-spec.md, file-impact-map.md §1). NO IMPLEMENTATION EXISTS YET —
these are feature tests derived from the acceptance criteria (rules/testing.md:
"a test derived from the code tests what the code does" — there is no code to
derive from, so this is naturally satisfied), not from a proposed fix.

AC-1/AC-2's core behavior (hold `.z` entries up to W=1.0s and release them in
TIMESTAMP order, watermark advanced ONLY by `.z` arrivals) is testable through
`StreamNormalizer`'s EXISTING public surface (`process_line`/`normalize`) —
the buffer is purely an internal change to `_process_z`; no new method is
needed to observe reorder/watermark behavior, only to observe the two things
that have no other trigger: the wall-clock backstop (nothing message-driven
exists to fire it, by definition — whole-feed silence) and the explicit
end-of-stream flush. Those two are marked ASSUMED-INTERFACE below, per
file-impact-map.md §1's own recommended shape (an injectable wall clock,
matching file_reader.py's existing `_now`/`_sleep` pattern, and a `flush()`
method) — the spec explicitly leaves the exact interface an open
implementation choice (Open Question #1), so these two are scaffolding: they
encode the ACCEPTANCE BEHAVIOR, and the exact call signature may need
adjusting to whatever interface the implementer actually picks, but the
behavior they assert does not.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.file_reader import RawLine
from app.processing.stream_normalizer import StreamNormalizer

import base64
import json
import zlib


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _line(topic: str, data, envelope_ts: str) -> RawLine:
    return RawLine(topic=topic, data=data, envelope_ts=_dt(envelope_ts))


def _compress(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = co.compress(raw) + co.flush()
    return base64.b64encode(compressed).decode("ascii")


def _car_data_z(ts: str, num: str = "1", speed: int = 200) -> str:
    return _compress({"Entries": [{"Utc": ts, "Cars": {num: {"Channels": {"2": speed}}}}]})


def _position_z(ts: str, num: str = "1", dp: float = 5.0) -> str:
    return _compress({"Position": [{"Timestamp": ts, "Entries": {num: [0, 0, dp]}}]})


class ReordersWithinTheWindow_AC1(unittest.TestCase):
    """A CarData.z entry with the LATER own-timestamp arrives (in FILE/
    envelope order) before a Position.z entry with an EARLIER own-timestamp,
    both within the 1.0s window — the exact "next envelope" shape
    architecture-plan.md §A.7.1 measured. Output RELEASE order must reflect
    TIMESTAMP order (Position before CarData), not arrival order."""

    def test_out_of_timestamp_order_arrival_is_released_in_timestamp_order_RED_GATE(self):
        norm = StreamNormalizer()
        out = []
        # CarData.z arrives FIRST (envelope), own ts = 10:00:00.900.
        out.extend(norm.process_line(_line(
            "CarData.z", _car_data_z("2026-07-18T10:00:00.900Z"), "2026-07-18T10:00:01.000Z")))
        # Position.z arrives SECOND (envelope), own ts = 10:00:00.100 — earlier.
        out.extend(norm.process_line(_line(
            "Position.z", _position_z("2026-07-18T10:00:00.100Z"), "2026-07-18T10:00:01.050Z")))
        # A later .z arrival (either topic) past the window advances the
        # watermark far enough to force release of both held entries.
        out.extend(norm.process_line(_line(
            "CarData.z", _car_data_z("2026-07-18T10:00:02.500Z"), "2026-07-18T10:00:02.600Z")))
        # The 3rd entry (the one that just arrived and set the new high-water
        # mark) cannot satisfy its own release criterion in the same step —
        # no causal, no-lookahead buffer can prove nothing earlier is still
        # coming until something else arrives afterward. `flush()` is that
        # "something else": the real end-of-stream release path (AC-3),
        # exercised here instead of a 4th synthetic process_line() call.
        out.extend(norm.flush())

        z_out = [m for m in out if m.topic in ("CarData.z", "Position.z")]
        self.assertEqual(len(z_out), 3, "all three .z entries must eventually be released")
        # Correct/fixed expectation: released in ascending payload-timestamp
        # order (Position 10:00:00.100 before CarData 10:00:00.900).
        timestamps = [m.utc_timestamp for m in z_out]
        self.assertEqual(
            timestamps, sorted(timestamps),
            f"released .z entries are not in ascending timestamp order: {timestamps} "
            f"— the reorder buffer does not exist yet, entries are released in "
            f"arrival (file) order instead",
        )


class WatermarkIsZOnly_AC2(unittest.TestCase):
    """AC-2's hard constraint: injecting a non-`.z` message between `.z`
    arrivals must not advance the buffer's release watermark or otherwise
    change .z release timing/order. NOTE: with no buffer implemented yet,
    there is nothing for a non-.z message to corrupt — this assertion holds
    VACUOUSLY today (nothing changes because nothing is buffered) and is
    retained as a forward guard: once AC-1 lands, this same test must keep
    passing, proving the watermark genuinely stays .z-only."""

    def test_non_z_message_between_z_arrivals_does_not_change_z_release_order(self):
        def run(inject_non_z: bool):
            norm = StreamNormalizer()
            out = []
            out.extend(norm.process_line(_line(
                "CarData.z", _car_data_z("2026-07-18T10:00:00.900Z"), "2026-07-18T10:00:01.000Z")))
            if inject_non_z:
                # A generic non-.z topic whose OWN (envelope-STREAM_LAG) clock
                # runs ahead of the .z payload clock (exactly the cross-clock
                # hazard AC-2 exists to prevent) must NOT release the held
                # CarData.z entry early.
                out.extend(norm.process_line(_line(
                    "TrackStatus", {"Status": "1"}, "2026-07-18T10:00:01.900Z")))
            out.extend(norm.process_line(_line(
                "Position.z", _position_z("2026-07-18T10:00:00.100Z"), "2026-07-18T10:00:01.050Z")))
            out.extend(norm.process_line(_line(
                "CarData.z", _car_data_z("2026-07-18T10:00:02.500Z"), "2026-07-18T10:00:02.600Z")))
            return [(m.topic, m.utc_timestamp) for m in out if m.topic in ("CarData.z", "Position.z")]

        without = run(inject_non_z=False)
        with_injected = run(inject_non_z=True)
        self.assertEqual(
            without, with_injected,
            "injecting a non-.z message between .z arrivals changed .z release "
            "order/timing — the watermark must be advanced ONLY by CarData.z/"
            "Position.z arrivals (AC-2)",
        )


class WallClockBackstop_AC3_AssumedInterface(unittest.TestCase):
    """AC-3's wall-clock backstop: during WHOLE-FEED silence (nothing arrives
    at all, by definition — the message-driven watermark check never fires),
    a held `.z` entry must still be released after a bounded wall-clock
    timeout. No message-driven trigger can demonstrate this — it requires an
    injectable wall clock, following this codebase's own established pattern
    (file_reader.py's `_now`/`_sleep` injection for `pace`).

    ASSUMED INTERFACE (Open Question #1 — no duration or exact call shape is
    settled by any source artifact): `StreamNormalizer(..., wall_clock_backstop_s=X,
    _now=callable)`, and a `poll_wall_clock()` (or equivalent) method the
    caller invokes periodically to release anything past the backstop. This
    test's assertions encode the ACCEPTANCE BEHAVIOR (a held entry is
    eventually released without further message input, bounded well below
    the ~87s worst-case observed stall); the exact constructor/method names
    are a placeholder for whatever the implementer actually builds and MUST
    be updated to match — see test-plan.md."""

    def test_held_entry_is_released_after_wall_clock_backstop_with_no_further_input(self):
        fake_now = [1000.0]

        def _now():
            return fake_now[0]

        try:
            norm = StreamNormalizer(wall_clock_backstop_s=90.0, _now=_now)
        except TypeError:
            self.fail(
                "StreamNormalizer does not accept a wall-clock backstop / "
                "injectable-clock constructor argument yet — AC-3's wall-clock "
                "backstop has not been implemented (assumed interface, see "
                "test-plan.md; the exact parameter names may differ once "
                "implemented — update this test to match)"
            )

        out = norm.process_line(_line(
            "CarData.z", _car_data_z("2026-07-18T10:00:00.000Z"), "2026-07-18T10:00:00.100Z"))
        self.assertEqual(out, [], "entry should be held in the buffer, not released immediately")

        # Whole-feed silence: no further messages arrive. Advance the FAKE
        # wall clock past the backstop and ask the normalizer to check.
        fake_now[0] += 95.0
        released = norm.poll_wall_clock_backstop()
        self.assertTrue(
            len(released) >= 1,
            "the held CarData.z entry must be released by the wall-clock "
            "backstop once wall time exceeds the configured timeout, even "
            "with zero further message input",
        )


class ExplicitFlush_AC3_AssumedInterface(unittest.TestCase):
    """AC-3's end-of-stream flush: per file-impact-map.md's correction, this
    must be triggered by exhaustion of the async iterator in preprocessor.py,
    NOT the `_SessionEnd` marker. At the StreamNormalizer level this needs an
    explicit method the caller invokes once input is exhausted — no message
    can trigger it (there is no more input, by construction).

    ASSUMED INTERFACE: `StreamNormalizer.flush()` returning whatever
    `NormalizedMessage`s remain buffered, gate/dedup-checked and stamped the
    same way `_process_z` does. See test-plan.md."""

    def test_flush_releases_everything_still_held_in_the_buffer(self):
        norm = StreamNormalizer()
        norm.process_line(_line(
            "CarData.z", _car_data_z("2026-07-18T10:00:00.000Z"), "2026-07-18T10:00:00.100Z"))
        norm.process_line(_line(
            "Position.z", _position_z("2026-07-18T10:00:00.050Z"), "2026-07-18T10:00:00.150Z"))

        self.assertTrue(
            hasattr(norm, "flush"),
            "StreamNormalizer has no flush() method yet — AC-3's end-of-stream "
            "flush (file-impact-map.md §1 AC-3) has not been implemented",
        )
        flushed = norm.flush()
        self.assertEqual(
            len(flushed), 2,
            "flush() must release every .z entry still held in the buffer "
            "at end of stream, not silently drop them",
        )


if __name__ == "__main__":
    unittest.main()
