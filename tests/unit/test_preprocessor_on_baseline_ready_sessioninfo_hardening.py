"""Regression coverage for N9 (review-findings.md, 2026-08-17-047 WB-1 resume
addendum; Trello card FMfHKZDY): `on_baseline_ready`'s timestamp-group-close
trigger (`preprocessor.py`, `filtered.utc_timestamp > self._start_time`)
assumes `SessionInfo` sits in the OPENING timestamp group. That is true for
all 21 golden fixtures this project holds (verified 2026-08-17), but nothing
in the code enforces it.

This test uses the exact fixture shape the review named as unguarded:
`TrackStatus @ t0`, `Heartbeat @ t1`, `SessionInfo @ t2` -- THREE distinct
timestamp groups, `SessionInfo` in the third, not the first. Under the
current trigger, the opening group (`TrackStatus @ t0` alone) closes the
moment `Heartbeat @ t1` arrives, `on_baseline_ready` fires there, and
`SessionInfo` (still two messages away) has not been processed or flushed
yet. That reopens card 77 (blank tiles at connect until a manual seek) --
the exact hazard `on_baseline_ready` exists to prevent -- in a shape the
existing M1 regression test (`BaselineReadyRequiresRealBaselineNotJustAnyRow`
in `test_preprocessor_on_baseline_ready_ordering.py`) does not cover, because
that fixture's `SessionInfo` shares `t0`, the opening group, with
`TrackStatus`.

Per `.claude/rules/testing.md`'s Red Gate: this test is written against the
UNFIXED code, from the requirement (the callback must not fire before the
baseline batch, including `sessionInfo`, is committed and queryable), not
from a proposed fix. It is expected to FAIL against the current trigger
logic -- see `test-plan.md` in this task's artifact directory for the
recorded red-gate evidence.
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
    """Duplicated from test_preprocessor_on_baseline_ready_ordering.py's
    `_Harness` rather than imported, so this red-gate test file stands alone
    and the existing (soon to be restructured) ordering-test file is not
    touched at all during this phase."""

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


class BaselineReadyRequiresSessionInfoEvenOutsideOpeningGroup(_Harness):
    """N9: `SessionInfo` in the THIRD timestamp group, not the first. The
    current trigger fires at the close of the FIRST group (t0 -> t1), which
    is two messages before `SessionInfo` (t2) has even been read."""

    async def test_baseline_ready_waits_for_sessioninfo_when_it_is_two_groups_later(self):
        track_status = {"Message": "AllClear"}  # no-op emit, same as the M1 fixture
        si = {
            "Key": 11330, "Type": "Qualifying", "Name": "Sprint Qualifying",
            "Meeting": {"Name": "Chinese Grand Prix",
                        "Circuit": {"ShortName": "Shanghai"}},
        }
        self._write_lines([
            _env("TrackStatus", "2026-03-13T07:17:19.130000Z", track_status),   # t0
            _env("Heartbeat", "2026-03-13T07:17:20.130000Z", {"n": 0}),          # t1
            _env("SessionInfo", "2026-03-13T07:17:21.130000Z", si),             # t2
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
            topics = {r[1] for r in rows}
            self.assertIn(
                "sessionInfo", topics,
                "on_baseline_ready fired before SessionInfo (2 timestamp "
                "groups later than the opening group) had been processed "
                "and flushed -- this is card 77 reopened: a connecting "
                "client's replay restore (session.py:535-545) waits on "
                "this exact callback and would serve get_state_at against "
                "a DB with no sessionInfo-derived baseline row, leaving "
                "tiles blank until a manual seek. The current trigger "
                "(fire on timestamp-group-close) assumes SessionInfo is in "
                "the OPENING group, which is true for all 21 golden "
                "fixtures but enforced nowhere in the code (N9, "
                "review-findings.md addendum).",
            )
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
