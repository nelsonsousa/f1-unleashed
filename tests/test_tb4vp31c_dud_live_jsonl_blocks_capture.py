"""Regression guard for tb4vp31c — a dud `live.jsonl` corpse from a crashed/failed
capture attempt silently blocks a later, correct attempt at the same deterministic
on-disk path, on both ends of the pipeline (`services-code-review.md` §2-3,
`docs/artifacts/2026-07-27-006-backend-review-replan/backend-synthesis.md` §1.4/R2):

1. `LiveTimingFetcher.fetch_session()` decides whether to skip a CDN re-download
   based on bare `live_file.exists()` — weaker than the same module's own
   `is_jsonl_complete()` (checks for the terminal `SessionStatus: Ends` marker) or
   `_dir_downloaded()` (checks non-zero size). A 0-byte or crash-truncated
   `live.jsonl` therefore permanently blocks re-download, because `.exists()` is
   true forever.

2. `F1SignalRClient._run_connection()` opens `live.jsonl` in unconditional append
   mode (`open(live_file, "a", ...)`) at a deterministic path, with no check that
   the existing content belongs to a genuinely-resumable prior attempt. A restarted
   capture therefore appends fresh data onto a failed predecessor's corpse (e.g. a
   file containing only the crash-time `_SessionEnd` marker with no real timing
   data), corrupting `live.jsonl` for anyone who replays it later.

Both tests are written against the CURRENT (unfixed) code and are expected to FAIL,
demonstrating the bug. Do not "fix" these tests to make them pass without first
fixing the corresponding production code.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import livetiming_fetcher as lf
from app.services.livetiming_fetcher import LiveTimingFetcher, Meeting, Session
from app.services.signalr_client import F1SignalRClient


class DudLiveJsonlBlocksCdnRedownloadTest(unittest.IsolatedAsyncioTestCase):
    """fetch_session() must not treat a dud/incomplete live.jsonl as "already
    cached" — it should behave like a fresh download, the same way it does when
    the file is entirely absent."""

    async def test_incomplete_live_jsonl_triggers_redownload_not_early_return(self):
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
        # The corpse of a crashed capture: present, non-empty, but nowhere near a
        # complete session (no terminal "SessionStatus": "Ends" marker) — exactly
        # what a mid-capture crash or truncated CDN fetch leaves behind.
        live_file.write_text(json.dumps({
            "Type": "_SessionEnd",
            "DateTime": "2026-01-01T00:00:00",
            "Json": {"MessageCount": 0},
        }) + "\n")

        with patch.object(lf.LiveTimingFetcher, "_fetch_json_file",
                           new=AsyncMock(return_value={})) as m_json, \
             patch.object(lf.LiveTimingFetcher, "_fetch_topic_data",
                           new=AsyncMock(return_value=[])) as m_topic, \
             patch.object(lf, "SessionPreProcessor", autospec=True) as pre_cls:
            pre_cls.return_value.failed = False
            await fetcher.fetch_session(2026, meeting, session, force=False)

        # A real re-download issues network fetches (SessionInfo, ExtrapolatedClock,
        # etc.). Against the unfixed code, fetch_session() early-returns on bare
        # .exists() and never calls these — this is the observable symptom of the
        # bug, and the assertion below is expected to FAIL against current code.
        self.assertTrue(
            m_json.await_count > 0 or m_topic.await_count > 0,
            "fetch_session() skipped re-download of a dud/incomplete live.jsonl "
            "based on bare .exists(), instead of detecting it as incomplete "
            "(cf. the module's own is_jsonl_complete()) and re-fetching",
        )


class RestartedCaptureAppendsOntoDudPredecessorTest(unittest.TestCase):
    """A fresh capture opening live.jsonl at a deterministic path must not blindly
    append onto a prior failed attempt's corpse."""

    def test_run_connection_does_not_append_onto_dud_predecessor(self):
        tmp = Path(tempfile.mkdtemp())
        cache_path = tmp / "2026" / "01_Test" / "11_Race"
        cache_path.mkdir(parents=True)
        live_file = cache_path / "live.jsonl"

        # Corpse of a crashed prior attempt at this SAME deterministic path —
        # unique marker so we can tell whether it survived the new capture.
        dud_marker = {
            "Type": "_SessionEnd",
            "DateTime": "2026-01-01T00:00:00",
            "Json": {"MessageCount": 0, "provenance": "OLD_FAILED_ATTEMPT"},
        }
        live_file.write_text(json.dumps(dud_marker) + "\n")

        client = F1SignalRClient(cache_path=cache_path, no_auth=True)
        client._is_running = True
        # Stub out the network/SignalR layer only — _run_connection's own file
        # lifecycle (open/append/finalize) is what's under test, unchanged from
        # production.
        client._connect_and_serve = lambda: "stopped"
        client._run_connection()

        contents = live_file.read_text()
        self.assertNotIn(
            "OLD_FAILED_ATTEMPT", contents,
            "a new capture at this deterministic path appended onto a dud "
            "predecessor's corpse instead of starting fresh",
        )


if __name__ == "__main__":
    unittest.main()
