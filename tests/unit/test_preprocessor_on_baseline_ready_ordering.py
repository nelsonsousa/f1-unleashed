"""Regression coverage for `on_baseline_ready`'s ordering guarantee
(2026-08-17-047 WB-1 resume, item 3; file-impact-map.md §3;
implementation-plan.md's "`on_baseline_ready` ordering" section).

Before this task, NO test anywhere asserted on `on_baseline_ready`'s name or
timing at all (file-impact-map.md §3, confirmed by grep across `app/` and
`tests/`): "the timing-sensitivity risk here is not 'a test will fail,' it's
'nothing will fail and the regression will be silent' if the new trigger
point fires before the baseline rows are actually flushed." A connecting
client's replay restore (`session.py:541`,
`asyncio.wait_for(self._baseline_ready.wait(), timeout=30.0)`) is gated on
this callback and must see COMMITTED rows, not merely "the gate opened" --
serving `get_state_at` before the baseline batch is on disk leaves tiles
blank until a manual seek.

The documented guarantee (implementation-plan.md): `on_baseline_ready` fires
on the first message that is BOTH (a) a universal-gate survivor and (b)
actually emitted post-`_filter_message` -- and firing is preceded by
`self._flush_buffer()` actually persisting that message's row to the DB.
`self._baseline_ready_fired` is a separate flag from `_start_time` that only
flips on an actual emit, so a first-survivor message that is itself later
dropped by `_filter_message` (RCM/SessionData payload-entry filtering) does
not fire the callback for a message that was never emitted.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _env(topic: str, dt: str, data) -> str:
    return json.dumps({"Type": topic, "DateTime": dt, "Json": data})


class _Harness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

    def _write_lines(self, lines: list[str]) -> None:
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    def _make(self) -> SessionPreProcessor:
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            return SessionPreProcessor(self.sess, "Qualifying")


class BaselineReadyFiresOnlyAfterFlush(_Harness):
    async def test_baseline_ready_fires_after_the_row_is_committed_not_before(self):
        """The core ordering guarantee: by the time `on_baseline_ready` is
        called, the first emitted message's row must already be queryable
        from the DB -- not merely "the gate is open, a flush will happen
        eventually". Verified by querying the DB from INSIDE the callback
        itself, the same way a connecting client's restore would."""
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        self._write_lines([
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", si),
        ])
        p = self._make()
        rows_visible_in_callback = []

        def _on_baseline_ready():
            rows_visible_in_callback.append(
                len(p._db.get_messages_in_range(-1, 10_000)))

        try:
            await p.run(on_baseline_ready=_on_baseline_ready)
            self.assertEqual(len(rows_visible_in_callback), 1,
                              "on_baseline_ready must fire exactly once")
            rows_after_run = len(p._db.get_messages_in_range(-1, 10_000))
            self.assertGreater(
                rows_visible_in_callback[0], 0,
                "at least the SessionInfo-derived baseline row(s) must "
                "already be committed and queryable at the moment "
                "on_baseline_ready fires -- not merely pending in the "
                "in-memory output buffer",
            )
            self.assertEqual(
                rows_visible_in_callback[0], rows_after_run,
                "every row this single-message session ever produces was "
                "already committed by the time on_baseline_ready fired -- "
                "nothing was still pending in the in-memory buffer",
            )
        finally:
            p._db.close()

    async def test_baseline_ready_fires_exactly_once_even_with_many_post_gate_messages(self):
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        lines = [_env("SessionInfo", "2026-07-18T10:00:00.000Z", si)]
        for i in range(5):
            lines.append(_env("Heartbeat", f"2026-07-18T10:00:0{i+1}.000Z", {"n": i}))
        self._write_lines(lines)
        p = self._make()
        call_count = {"n": 0}

        def _on_baseline_ready():
            call_count["n"] += 1

        try:
            await p.run(on_baseline_ready=_on_baseline_ready)
            self.assertEqual(call_count["n"], 1)
            self.assertTrue(p._baseline_ready_fired)
        finally:
            p._db.close()

    async def test_baseline_ready_never_fires_if_no_message_is_ever_emitted(self):
        """An empty capture (no message ever reaches the loop) must not
        fire the callback at all -- there is no baseline to signal."""
        self._write_lines([])
        p = self._make()
        called = {"n": 0}

        def _on_baseline_ready():
            called["n"] += 1

        try:
            await p.run(on_baseline_ready=_on_baseline_ready)
            self.assertEqual(called["n"], 0)
            self.assertFalse(p._baseline_ready_fired)
        finally:
            p._db.close()


class BaselineReadySurvivesGateWithoutSurvivingFilterMessage(_Harness):
    """`_start_time`/`_cutoff` are set unconditionally on the first
    universal-gate survivor (even if it is later dropped by
    `_filter_message`'s own stale-entry filtering) -- but
    `_baseline_ready_fired` is a SEPARATE flag that only flips on an actual
    emit. This proves the two are decoupled: a first-survivor message that
    `_filter_message` itself drops must not fire `on_baseline_ready` for
    nothing ever having reached the bus (rare in practice -- RCM/SessionData
    payload-entry filtering only, never the envelope-level cutoff, which a
    message can never fail against a cutoff computed FROM itself)."""

    async def test_first_survivor_dropped_by_filter_message_does_not_fire_early(self):
        # First message: RaceControlMessages whose SOLE entry is timestamped
        # 2h before the message's own envelope -- `_cutoff` is computed AS
        # (this message's own utc_timestamp - 1h), so the entry (2h before)
        # is stale relative to that self-referential cutoff and
        # `_filter_payload_timestamps` strips it to an empty dict,
        # `_filter_message` returns None for the whole message: `_start_time`
        # gets set (unconditional, before filtering) but NOTHING is ever
        # emitted for it.
        rcm = {"Messages": {"1": {"Utc": "2026-07-18T08:00:00.000Z", "Message": "stale"}}}
        lines = [
            _env("RaceControlMessages", "2026-07-18T10:00:00.000Z", rcm),
            # Second message, well within the cutoff -- the actual first
            # EMIT, and where on_baseline_ready must fire.
            _env("Heartbeat", "2026-07-18T10:00:05.000Z", {}),
        ]
        self._write_lines(lines)
        p = self._make()
        fired_at_message_counts = []

        def _on_baseline_ready():
            fired_at_message_counts.append(p._message_count)

        try:
            await p.run(on_baseline_ready=_on_baseline_ready)
            self.assertFalse(p.failed)
            # `_start_time` anchors to the FIRST survivor (the RCM message),
            # even though that message itself was later dropped by
            # `_filter_message`.
            self.assertEqual(p._db.get_meta("start_time"), "2026-07-18T10:00:00+00:00")
            # on_baseline_ready fired exactly once, and only once the
            # Heartbeat (the actual first EMIT) had been counted -- not for
            # the RCM message, which was dropped and never emitted.
            self.assertEqual(fired_at_message_counts, [1])
            rows = p._db.get_messages_in_range(-1, 10_000)
            heartbeat_rows = [r for r in rows if r[1] == "heartbeat"]
            self.assertEqual(len(heartbeat_rows), 1)
        finally:
            p._db.close()


class BaselineReadyRequiresRealBaselineNotJustAnyRow(_Harness):
    """M1 (review-findings.md, 2026-08-17-047 WB-1 resume, `change-reviewer`
    opus). The human's confirmed decision was that `on_baseline_ready` fires
    "at the same logical moment... ordered after the baseline batch is
    flushed to DB, not merely on gate-survival" -- i.e. not merely once ANY
    message is emitted, but once the baseline (SessionInfo-derived data +
    DriverList) is actually queryable.

    A real capture's first gate survivor is frequently a topic that produces
    ZERO persisted rows -- exactly the shape review reproduced in
    `regression/golden/shanghai-sq-cdn`: TrackStatus arrives at the same
    timestamp as, and immediately before, SessionInfo. Review measured 0 rows
    committed at the moment `on_baseline_ready` fired on this branch (5 on
    `test`). The two tests above in this file both use a fixture whose FIRST
    line IS SessionInfo, so they cannot distinguish "fires after the first
    emit" (what got built) from "fires after the baseline batch is flushed"
    (what was decided) -- this test is the one that can.
    """

    async def test_baseline_ready_waits_for_sessioninfo_row_when_trackstatus_arrives_first(self):
        # First gate survivor: TrackStatus whose Message is a no-op for
        # TrackStatusProcessor. "AllClear" only takes effect while racing
        # (`self._status` starts as "" -- not in ("sc", "vsc")), so
        # `_handle_track_status` returns having emitted NOTHING to the bus.
        # The raw envelope message still survives the universal gate and is
        # still "the first message actually emitted" as `preprocessor.py`
        # currently defines that phrase (`self._bus.emit(filtered.topic,
        # ...)` happens unconditionally for any gate/filter survivor) -- but
        # it produces zero persisted output rows.
        track_status = {"Message": "AllClear"}
        # Second message, same timestamp (matches the real corpus shape
        # review found): SessionInfo, which produces the real baseline rows
        # (meetingName, trackCircuit, sessionBadge, sessionInfo) via
        # SessionInfoProcessor.
        si = {
            "Key": 11330, "Type": "Qualifying", "Name": "Sprint Qualifying",
            "Meeting": {"Name": "Chinese Grand Prix",
                        "Circuit": {"ShortName": "Shanghai"}},
        }
        self._write_lines([
            _env("TrackStatus", "2026-03-13T07:17:19.130000Z", track_status),
            _env("SessionInfo", "2026-03-13T07:17:19.130000Z", si),
        ])
        p = self._make()
        rows_visible_in_callback = []

        def _on_baseline_ready():
            rows_visible_in_callback.append(
                p._db.get_messages_in_range(-1, 10_000))

        try:
            await p.run(on_baseline_ready=_on_baseline_ready)
            self.assertEqual(len(rows_visible_in_callback), 1,
                              "on_baseline_ready must fire exactly once")
            rows = rows_visible_in_callback[0]
            self.assertGreater(
                len(rows), 0,
                "on_baseline_ready fired while ZERO rows were committed to "
                "the DB -- this is card 77 reopened: a connecting client's "
                "replay restore (session.py:535-545) waits on this exact "
                "callback and would serve get_state_at against an empty "
                "DB, leaving tiles blank until a manual seek.",
            )
            topics = {r[1] for r in rows}
            self.assertIn(
                "sessionInfo", topics,
                "the baseline batch (SessionInfo-derived data) must already "
                "be committed and queryable by the time on_baseline_ready "
                "fires. Firing on the first EMITTED message (here, "
                "TrackStatus, which produces no output row of its own) is "
                "not equivalent to firing once the baseline batch itself "
                "is flushed to DB -- the human's explicit decision.",
            )
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
