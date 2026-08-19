"""Trello lIGWChiB: "A mid-session restart silently truncates live.jsonl,
discarding the entire pre-restart capture."

Root cause (see docs/artifacts/2026-08-19-066-restart-during-live-session/
data-investigation.md at the project root for the full empirical evidence):
`F1SignalRClient._run_connection` decided append-vs-truncate once, at file-open
time, by asking `is_jsonl_complete(live_file)` — which looks for the literal
F1 SessionStatus terminal marker `"Status": "Ends"`. A capture stopped
mid-session (crash, hard kill, or a graceful `stop()` — e.g. the app's own
`lifespan` shutdown handler stopping an active capture on restart) never
receives that marker, so the file reads as "the corpse of a crashed attempt"
and the NEXT process's `F1SignalRClient` truncates it — discarding everything
already captured.

Fix (human-specified, 2026-08-19): an out-of-band empty marker file (`.live`,
`LIVE_MARKER_FILENAME` in `app/services/livetiming_fetcher.py`) is created in
the cache directory when a capture genuinely starts, removed only when the
session genuinely, cleanly ends (a real SessionStatus="Ends" message was
observed), and consulted instead of `is_jsonl_complete()` for the
append-vs-truncate decision. Marker present -> append (a live process
previously claimed this path, possibly interrupted -> don't destroy it).
Marker absent -> overwrite (genuinely new session, or a file with no
active-live provenance -- matches the pre-fix "corpse" semantics, now driven
by an explicit signal instead of fragile content-parsing).

The FIRST test class below (`MidSessionStopDoesNotTruncateNextOpen`) is the
Red Gate regression test: written against the requirement, run against the
UNFIXED code first, and observed to fail for the right reason (data lost)
before any production code changed. See test-plan.md / verification-report.md
in the task's artifact directory for the recorded red-gate evidence.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, patch

from app.services import livetiming_fetcher as lf
from app.services.livetiming_fetcher import (
    LIVE_MARKER_FILENAME,
    LiveTimingFetcher,
    Meeting,
    Session,
)
from app.services.signalr_client import F1SignalRClient


def _make_client(cache_path: Path) -> F1SignalRClient:
    client = F1SignalRClient(cache_path=cache_path, no_auth=True)
    client._is_running = True
    return client


class MidSessionStopDoesNotTruncateNextOpen(unittest.TestCase):
    """Red Gate: a capture that stops mid-session (no real SessionStatus=Ends
    observed -- simulating a crash, hard kill, or a graceful restart-time
    stop) must not have its data discarded when the NEXT F1SignalRClient
    instance (a new process/new object, same deterministic cache_path) opens
    live.jsonl. This is the exact scenario lIGWChiB reports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "2026" / "01_Test" / "11_Race"
        self.cache_path.mkdir(parents=True)
        self.live_file = self.cache_path / "live.jsonl"

    def test_data_survives_a_mid_session_stop_and_reopen(self):
        # --- "process 1": captures real data, then stops WITHOUT the session
        # having genuinely ended (no SessionStatus=Ends ever arrives) ---
        client1 = _make_client(self.cache_path)

        def fake_serve_1():
            client1._process_message(
                "CarData.z", {"provenance": "PRE_RESTART_DATA"},
                datetime.now(timezone.utc).replace(tzinfo=None),
            )
            return "stopped"  # graceful stop, e.g. app shutdown mid-session

        client1._connect_and_serve = fake_serve_1
        client1._run_connection()

        contents_after_first = self.live_file.read_text()
        self.assertIn(
            "PRE_RESTART_DATA", contents_after_first,
            "sanity check: the first process's data was not even written",
        )

        # --- "process 2": a brand-new F1SignalRClient at the SAME
        # deterministic cache_path (simulating the server restarting and
        # live_capture starting a fresh capture for the still-live session) ---
        client2 = _make_client(self.cache_path)
        client2._connect_and_serve = lambda: "stopped"
        client2._run_connection()

        contents_after_second = self.live_file.read_text()
        self.assertIn(
            "PRE_RESTART_DATA", contents_after_second,
            "a mid-session stop's data was truncated by the next process's "
            "file open -- this is exactly the lIGWChiB bug",
        )


class LiveMarkerLifecycle(unittest.TestCase):
    """Direct tests of the marker file's creation, persistence, and removal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "2026" / "01_Test" / "11_Race"
        self.cache_path.mkdir(parents=True)
        self.live_file = self.cache_path / "live.jsonl"
        self.marker_file = self.cache_path / LIVE_MARKER_FILENAME

    def test_marker_created_when_a_new_capture_session_starts(self):
        client = _make_client(self.cache_path)
        client._connect_and_serve = lambda: "stopped"
        # Marker must exist once the file is opened for writing (mid-run),
        # before finalize has any chance to remove it.
        original_finalize = client._finalize_capture
        marker_seen_mid_run = {}

        def spy_finalize():
            marker_seen_mid_run["present"] = self.marker_file.exists()
            original_finalize()

        client._finalize_capture = spy_finalize
        client._run_connection()
        self.assertTrue(
            marker_seen_mid_run["present"],
            "marker was not created when the capture session started",
        )

    def test_marker_persists_after_a_stop_with_no_genuine_session_end(self):
        client = _make_client(self.cache_path)
        client._connect_and_serve = lambda: "stopped"
        client._run_connection()
        self.assertTrue(
            self.marker_file.exists(),
            "marker was removed even though no real SessionStatus=Ends was "
            "observed -- a mid-session stop must leave it in place",
        )

    def test_marker_removed_after_a_genuine_session_end(self):
        client = _make_client(self.cache_path)

        def fake_serve():
            client._process_message(
                "SessionStatus", {"Status": "Ends"},
                datetime.now(timezone.utc).replace(tzinfo=None),
            )
            return "stopped"

        client._connect_and_serve = fake_serve
        client._run_connection()
        self.assertFalse(
            self.marker_file.exists(),
            "marker survived a genuine, clean session end -- it should have "
            "been removed at finalize",
        )

    def test_marker_absent_and_no_prior_file_opens_fresh_in_overwrite_mode(self):
        client = _make_client(self.cache_path)
        client._connect_and_serve = lambda: "stopped"
        client._run_connection()
        # No prior marker, no prior file -> "w" is used; content is exactly
        # what this run wrote (plus the synthetic _SessionEnd marker), no
        # foreign content survives (there was none to survive).
        self.assertTrue(self.live_file.exists())

    def test_absent_marker_with_a_stale_foreign_file_truncates_it(self):
        # A foreign/stale live.jsonl with no active-live provenance (e.g. a
        # leftover from an entirely unrelated failed attempt whose marker
        # was already cleaned up, or content this test seeds directly).
        self.live_file.write_text('{"Type": "FOREIGN", "Json": {}}\n')
        client = _make_client(self.cache_path)
        client._connect_and_serve = lambda: "stopped"
        client._run_connection()
        contents = self.live_file.read_text()
        self.assertNotIn(
            "FOREIGN", contents,
            "a stale file with no marker should be overwritten, not "
            "appended onto",
        )

    def test_present_marker_causes_append_even_if_content_looks_incomplete(self):
        # Marker present (this cache_path had an active/interrupted live
        # process) + file content with no terminal marker at all -> append,
        # not truncate, regardless of what is_jsonl_complete() would have said.
        self.live_file.write_text('{"Type": "RealData", "Json": {}}\n')
        self.marker_file.touch()
        client = _make_client(self.cache_path)
        client._connect_and_serve = lambda: "stopped"
        client._run_connection()
        contents = self.live_file.read_text()
        self.assertIn("RealData", contents)


class OtherIsJsonlCompleteCallSitesUnaffected(unittest.TestCase):
    """The two OTHER call sites of is_jsonl_complete() (the cache-browser
    "has_jsonl" recording-status icon, and fetch_session's force-skip
    CDN-re-download gate) must keep using content-based completeness --
    they are asking a different, still-valid question for CDN files, which
    never have (and never will have) a `.live` marker."""

    def test_livetiming_fetcher_still_calls_is_jsonl_complete_for_has_jsonl_and_force_gate(self):
        import inspect

        from app.services import livetiming_fetcher as lf

        src = inspect.getsource(lf)
        self.assertIn('info["has_jsonl"] = live_file.exists() and is_jsonl_complete(live_file)', src)
        self.assertIn("if not force and is_jsonl_complete(live_file):", src)


class CdnDownloadPathDoesNotCreateOrGetConfusedByTheMarker(unittest.IsolatedAsyncioTestCase):
    """Per the human's framing ("this only applies to live streams... CDN
    downloaded content doesn't have the issue"): a CDN fetch never creates
    or checks the `.live` marker, and if a stale marker is left behind at
    the same cache_path by an earlier, interrupted live capture, a CDN
    re-download must not get confused by it -- the CDN write overwrites
    live.jsonl unconditionally and cleans the stale marker up."""

    async def test_fetch_session_removes_a_stale_marker_when_it_overwrites_live_jsonl(self):
        tmp = Path(tempfile.mkdtemp())
        fetcher = LiveTimingFetcher()
        fetcher.cache_dir = tmp

        meeting = Meeting(key=1, name="Test GP", official_name="Test GP",
                           location="Testville", country="Testland",
                           circuit="Test Circuit", number=1)
        session = Session(key=11, name="Race", type="Race",
                           path="2026/test/", start_date=None, end_date=None,
                           gmt_offset=None)

        cache_dir = fetcher._get_session_cache_dir(2026, meeting, session)
        cache_dir.mkdir(parents=True, exist_ok=True)
        live_file = cache_dir / "live.jsonl"
        marker_file = cache_dir / LIVE_MARKER_FILENAME

        # Stale marker left behind by an earlier, interrupted live capture
        # at this same deterministic cache_path.
        marker_file.touch()
        live_file.write_text(json.dumps({
            "Type": "PRE_RESTART_LIVE_DATA", "Json": {}, "DateTime": "2026-01-01T00:00:00",
        }) + "\n")

        with patch.object(lf.LiveTimingFetcher, "_fetch_json_file",
                           new=AsyncMock(return_value={})), \
             patch.object(lf.LiveTimingFetcher, "_fetch_topic_data",
                           new=AsyncMock(return_value=[])), \
             patch.object(lf, "SessionPreProcessor", autospec=True) as pre_cls:
            pre_cls.return_value.failed = False
            await fetcher.fetch_session(2026, meeting, session, force=True)

        self.assertFalse(
            marker_file.exists(),
            "a stale .live marker survived a CDN download overwriting "
            "live.jsonl at the same path",
        )
        self.assertNotIn("PRE_RESTART_LIVE_DATA", live_file.read_text())

    def test_fetch_session_source_never_references_the_live_marker_for_gating(self):
        # Belt-and-braces source check: the force-skip / has_jsonl gates stay
        # content-based (is_jsonl_complete), not marker-based -- a CDN file
        # never has a `.live` marker and never will.
        import inspect

        src = inspect.getsource(lf)
        # The only two mentions of LIVE_MARKER_FILENAME in this module are
        # its definition and the defensive cleanup in fetch_session's write
        # path -- never in is_jsonl_complete, get_cached_sessions, or the
        # force-gate.
        self.assertEqual(src.count("LIVE_MARKER_FILENAME"), 2)


if __name__ == "__main__":
    unittest.main()
