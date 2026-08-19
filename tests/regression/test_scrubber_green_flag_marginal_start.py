"""Regression test for Trello card IIYYPBxZ ("First green flag of an FP
session isn't showing in the scrubber").

Root cause (docs/artifacts/2026-08-20-072-fp-first-green-flag-missing-scrubber/
data-investigation.md, project root): `preprocessor.py`'s scrubber-event
filter drops any `event` marker whose normalized `clock_time`
(`envelope_ts - STREAM_LAG`) falls strictly before the session's scheduled
start. For Practice/Qualifying, the session-start green flag IS the scheduled
start -- F1 publishes `SessionStatus=Started` within ~50-230ms of it, and the
`STREAM_LAG` estimate (carried over from the most recent `ExtrapolatedClock`
message, a DIFFERENT topic) carries ~150ms of noise on live captures. The
comparison is therefore a coin flip, not a real pre-session/post-session
distinction.

This test drives `SessionPreProcessor.run()` end to end (not just a direct
`_capture_output` call) through the real ingest pipeline -- StreamNormalizer's
STREAM_LAG estimation, SessionInfoProcessor's scheduled-start derivation, and
TrackStatusProcessor's GREEN emission -- against three fixtures built from the
EXACT real envelope/payload timestamps the investigation's rebuild used
(§5.5, §5.6's Budapest Q citation), reproducing the real margins bit for bit:

  - Spa FP1 (`11327_Practice_1`): margin +128ms -- CONTROL, must keep GREEN
    under both unfixed and fixed code (already passes; a change here would be
    a NEW regression, not the one this test guards).
  - Spa FP3 (`11329_Practice_3`): margin -16ms -- BUG REPRODUCTION, loses
    GREEN under unfixed code (Red Gate), must keep it after the fix.
  - Budapest Qualifying (liveStream capture, `11338_Qualifying`): margin
    -34ms -- confirms the same fix also resolves the Qualifying exposure the
    investigation found (§5.3), not just Practice.

A fourth case (`test_genuine_pre_session_noise_still_suppressed`) is
NON-regression coverage for the fix direction chosen (a grace window on the
threshold, not an unconditional exemption of the first green marker): a
`SessionStatus=Started` arriving 10 minutes before the scheduled start --
comfortably beyond the ~150ms clock-residual noise band and short of the
30-60 minute pit-lane-open noise the filter was built for in the first place
(2ec7bdd9, 2026-06-13) -- must still be suppressed. Without this case, a fix
that simply widened or removed the threshold would pass the three cases above
while silently reopening the exact noise the filter exists to keep out.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _line(topic: str, envelope_dt: str, payload: dict) -> str:
    return json.dumps({"Type": topic, "DateTime": envelope_dt, "Json": payload})


def _session_info(status: str, start_date: str, gmt_offset: str, key: int = 99001,
                   session_type: str = "Practice", number: int = 1,
                   name: str = "Practice 1") -> dict:
    return {
        "Key": key,
        "Type": session_type,
        "Number": number,
        "Name": name,
        "SessionStatus": status,
        "StartDate": start_date,
        "GmtOffset": gmt_offset,
        "Meeting": {"Name": "Test Grand Prix",
                    "Circuit": {"ShortName": "Test Circuit"}},
    }


class _Harness(unittest.IsolatedAsyncioTestCase):
    """Builds a real SessionPreProcessor over a minimal, real-timestamp
    live.jsonl and reads the `event` rows straight out of the resulting
    SQLite, exactly as `session.py`'s consumers do (`SELECT ... WHERE topic =
    'event' ORDER BY offset_ms`)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "Test_GP" / "99001_Practice_1"
        self.sess.mkdir(parents=True)

    def _write_lines(self, lines: list[str]) -> None:
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    def _make(self) -> SessionPreProcessor:
        with mock.patch("app.processing.database.transient_db_path",
                         return_value=self.root / "session.db"):
            return SessionPreProcessor(self.sess, "Practice")

    async def _run_and_get_event_rows(self, lines: list[str]) -> list[str]:
        self._write_lines(lines)
        p = self._make()
        try:
            await p.run()
            conn: sqlite3.Connection = p._db._conn
            rows = conn.execute(
                "SELECT data FROM messages WHERE topic = 'event' ORDER BY offset_ms"
            ).fetchall()
            return [json.loads(r[0]) for r in rows]
        finally:
            p._db.close()


class SpaFP1GreenFlagKeptControl(_Harness):
    """Margin +128ms (real capture timestamps, backups/livejsonl-20260725-134349/
    live/2026/1290_Spa-Francorchamps/11327_Practice_1/live.jsonl, lines 5/6/1552).
    Already survives the unfixed filter -- proves the fix does not need to
    (and must not) change this session's outcome."""

    async def test_green_flag_marker_is_present(self):
        lines = [
            _line("ExtrapolatedClock", "2026-07-17T11:17:15.492795",
                  {"Utc": "2026-07-17T11:17:13.5257719Z", "Remaining": "01:00:00"}),
            _line("SessionInfo", "2026-07-17T11:17:15.492795",
                  _session_info("Inactive", "2026-07-17T13:30:00", "02:00:00")),
            _line("SessionStatus", "2026-07-17T11:30:02.095054",
                  {"Status": "Started", "Started": "Started"}),
        ]
        events = await self._run_and_get_event_rows(lines)
        self.assertIn("GREEN", events, "Spa FP1 (control, +128ms margin) must "
                       "keep its session-start GREEN marker")


class SpaFP3GreenFlagDroppedRedGate(_Harness):
    """Margin -16ms (real capture timestamps, backups/livejsonl-20260725-134349/
    live/2026/1290_Spa-Francorchamps/11329_Practice_3/live.jsonl, lines 5/6/1552).
    The bug's exact reproduction: investigation §5.5 rebuilt this session
    with unfixed code and found NO green flag row in the table at all."""

    async def test_green_flag_marker_is_present_after_fix(self):
        lines = [
            _line("ExtrapolatedClock", "2026-07-18T10:17:53.875313",
                  {"Utc": "2026-07-18T10:17:51.8908559Z", "Remaining": "01:00:00"}),
            _line("SessionInfo", "2026-07-18T10:17:53.875313",
                  _session_info("Inactive", "2026-07-18T12:30:00", "02:00:00",
                                 key=99003, number=3, name="Practice 3")),
            _line("SessionStatus", "2026-07-18T10:30:01.968622",
                  {"Status": "Started", "Started": "Started"}),
        ]
        events = await self._run_and_get_event_rows(lines)
        self.assertIn("GREEN", events, "Spa FP3 (-16ms margin) must keep its "
                       "session-start GREEN marker once the fix is in place")


class BudapestQualifyingLiveStreamDroppedAtRisk(_Harness):
    """Margin -34ms (real capture timestamps, data/devData/livetiming_cache/2026/
    1291_Budapest-liveStream/11338_Qualifying/live.jsonl, lines 5/6/1573). The
    SAME real session captured live (+0.016s) and via CDN (+0.056s) keeps its
    marker; only the liveStream capture path loses it -- confirms Qualifying
    is exposed to the identical failure the investigation found for FP, not
    just a Practice-specific quirk."""

    async def test_green_flag_marker_is_present_after_fix(self):
        lines = [
            _line("ExtrapolatedClock", "2026-07-25T13:47:43.094697",
                  {"Utc": "2026-07-25T13:47:41.0998378Z", "Remaining": "00:18:00"}),
            _line("SessionInfo", "2026-07-25T13:47:43.094697",
                  _session_info("Inactive", "2026-07-25T16:00:00", "02:00:00",
                                 key=99038, session_type="Qualifying",
                                 number=0, name="Qualifying")),
            _line("SessionStatus", "2026-07-25T14:00:01.960442",
                  {"Status": "Started", "Started": "Started"}),
        ]
        events = await self._run_and_get_event_rows(lines)
        self.assertIn("GREEN", events, "Budapest Qualifying (liveStream "
                       "capture, -34ms margin) must keep its session-start "
                       "GREEN marker once the fix is in place")


class GenuinePreSessionNoiseStillSuppressed(_Harness):
    """Non-regression: the filter's original purpose (2ec7bdd9, 2026-06-13)
    was to suppress a `SessionStatus=Started` firing well before the
    scheduled start (an "implicit-GREEN at pit-exit-open" per the filter's
    own comment) -- a real, minutes-to-tens-of-minutes-early signal, not
    clock-estimate noise. A GREEN arriving 10 minutes early is far outside
    the ~150ms STREAM_LAG residual band this bug is about, and must remain
    suppressed however the marginal-timing fix is implemented."""

    async def test_green_flag_ten_minutes_early_is_still_dropped(self):
        lines = [
            _line("ExtrapolatedClock", "2026-07-18T10:17:53.875313",
                  {"Utc": "2026-07-18T10:17:51.8908559Z", "Remaining": "01:00:00"}),
            _line("SessionInfo", "2026-07-18T10:17:53.875313",
                  _session_info("Inactive", "2026-07-18T12:30:00", "02:00:00")),
            # SessionStatus=Started ~10 minutes before the 10:30:00 UTC
            # scheduled start (pit-exit-open style early signal).
            _line("SessionStatus", "2026-07-18T10:20:01.968622",
                  {"Status": "Started", "Started": "Started"}),
        ]
        events = await self._run_and_get_event_rows(lines)
        self.assertNotIn("GREEN", events, "a GREEN arriving ~10 minutes "
                          "before the scheduled start is genuine pre-session "
                          "noise and must stay suppressed")


if __name__ == "__main__":
    unittest.main()
