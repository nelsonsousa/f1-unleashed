"""WB-4 (R1, backend-synthesis.md §1.4 / test-battery-plan.md §6.2) -- RED GATE.

`SessionPreProcessor.run()` (app/processing/preprocessor.py:498-504) catches every
internal exception, sets `self.failed = True`, and RETURNS NORMALLY instead of
raising. Only one of its four call sites (`SessionEngine._run_preprocess`, pinned
by tests/test_preprocess_failure_surfaced.py) actually checks `.failed`
afterward. The other three -- `livetiming_fetcher.fetch_session`,
`live_capture._capture_loop`, and `utils/scripts/reprocess_year.py` (+ its
wrapper `reprocess_all.py`) -- treat "run() didn't raise" as "the build
succeeded".

This test exercises `LiveTimingFetcher.fetch_session` (livetiming_fetcher.py
:758-761), one of the three non-checking call sites named directly in R1. The
other obvious candidate, `utils/scripts/reprocess_year.py` (named explicitly
in test-battery-plan.md §6.2's example), currently cannot even be imported --
it references a module that does not exist anywhere in this codebase
(`from app.services.cache_manager import cache_manager`, reprocess_year.py:32,
confirmed absent via `grep -rn cache_manager`). That import failure is a
separate, pre-existing defect, out of this task's scope (WB-4 covers R1's
failure-semantics bug, not reprocess_year's broken import), so it is not a
usable target for this regression test today.

Mechanism: `run()`'s main message loop is a single broad `try/except Exception`
(preprocessor.py:344-504). A malformed *.jsonl* line alone will not reach it --
`file_reader.read_jsonl` already swallows `json.JSONDecodeError` per-line and
continues (file_reader.py:185-188) -- so this test injects the internal
failure at a clean seam instead: `SessionMessageBus.emit`, the call every
processed message is routed through (preprocessor.py:385, :446), is made to
raise for one topic partway through the build. That reproduces exactly the
shape of bug R1 describes: an exception from deep inside message processing,
caught by run()'s blanket handler, silently swallowed. HTTP calls are stubbed
(same technique as tests/test_download_post_build_call.py) so the fetch itself
never touches the network; `SessionPreProcessor` is NOT mocked away -- the real
class runs against the real fixture data so the swallow-and-continue mechanism
is genuinely exercised, not merely assumed.

Expected RED (against unfixed code): `fetch_session()` returns normally
(`cache_dir`, no exception) even though the underlying build set
`pre.failed = True`. A caller with no reason to inspect `.failed` -- which is
every caller of `fetch_session()` today -- cannot tell this build apart from a
genuinely successful one.

Do NOT fix `preprocessor.py` or any of the four caller files to make this test
pass -- see WB-4 in test-battery-plan.md and the Red Gate rule in
.claude/rules/testing.md. This test must be observed failing first.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.processing.message_bus import SessionMessageBus
from app.processing.preprocessor import SessionPreProcessor as RealSessionPreProcessor
from app.services import livetiming_fetcher as lf
from app.services.livetiming_fetcher import LiveTimingFetcher, Meeting, Session

_ORIG_EMIT = SessionMessageBus.emit


def _emit_that_fails_on_track_status(self, topic, data, clock_time):
    """Stand-in for a processor bug: raises partway through a real build,
    exactly the kind of internal exception run()'s try/except swallows."""
    if topic == "TrackStatus":
        raise RuntimeError("simulated processor failure mid-run (WB-4 injection)")
    return _ORIG_EMIT(self, topic, data, clock_time)


class Wb4LivetimingFetcherRunFailureSemantics(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_session_reports_success_for_a_build_that_actually_failed(self):
        tmp = Path(tempfile.mkdtemp())
        fetcher = LiveTimingFetcher()
        fetcher.cache_dir = tmp

        meeting = Meeting(key=1290, name="Test GP", official_name="Test GP",
                          location="Testville", country="Testland",
                          circuit="Test Circuit", number=1)
        # session.key=11330 is the numeric prefix on the cache folder name,
        # which SessionPreProcessor uses as its gate key (_extract_session_key) --
        # must match SessionInfo's own "Key" below for the gate to open at all.
        session = Session(key=11330, name="Qualifying", type="Qualifying",
                          path="2026/test/", start_date=None, end_date=None,
                          gmt_offset=None)

        t0 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}

        async def _fetch_json_file(self, url_prefix, name):
            if name == "SessionInfo":
                return si
            return {}   # Index.json etc: no extra topics discovered

        async def _fetch_topic_data(self, url_prefix, topic, session_start=None):
            if topic == "SessionInfo":
                return [{"topic": "SessionInfo", "timestamp": t0,
                         "offset": timedelta(0), "data": si}]
            if topic == "TrackStatus":
                # Arrives after the gate opens -- a normal, well-formed
                # message, so the build is genuinely partway through when the
                # injected failure fires (not a degenerate SessionInfo-only
                # build, per tests/test_preprocess_degenerate.py's distinct
                # concern).
                return [{"topic": "TrackStatus", "timestamp": t0 + timedelta(milliseconds=500),
                         "offset": timedelta(milliseconds=500),
                         "data": {"Status": "1", "Message": "AllClear"}}]
            return []

        captured: dict[str, RealSessionPreProcessor] = {}

        def _tracking_preprocessor(*args, **kwargs):
            # The real preprocessor, not a mock -- run() must actually execute
            # and actually swallow the injected exception. Stashed so the
            # test can confirm the injection worked (sanity), separately from
            # asserting what fetch_session() did with the outcome.
            inst = RealSessionPreProcessor(*args, **kwargs)
            captured["pre"] = inst
            return inst

        with mock.patch.object(lf.LiveTimingFetcher, "_fetch_json_file", new=_fetch_json_file), \
             mock.patch.object(lf.LiveTimingFetcher, "_fetch_topic_data", new=_fetch_topic_data), \
             mock.patch.object(lf, "SessionPreProcessor", new=_tracking_preprocessor), \
             mock.patch.object(SessionMessageBus, "emit", new=_emit_that_fails_on_track_status), \
             mock.patch("app.processing.database.transient_db_path",
                         return_value=tmp / "_scratch" / "session.db"):

            # RIGHT (post-fix): a build that hit an internal exception
            # mid-run() must not be indistinguishable from a successful one --
            # at minimum, fetch_session() must not return normally as if
            # nothing happened (R1's own proposed disposition leads with
            # "raise by default").
            #
            # WRONG (today, unfixed): run() swallows the RuntimeError
            # internally, sets self.failed = True, and returns normally.
            # fetch_session() never reads `pre.failed` after
            # `await pre.run(force=True)` (livetiming_fetcher.py:758-761) --
            # it logs "Session processed" and returns cache_dir exactly as it
            # would for a real successful build.
            with self.assertRaises(
                Exception,
                msg="fetch_session() must not return normally when the "
                    "underlying build failed partway through run() -- it did "
                    "today, silently reporting the session as usable"
            ):
                await fetcher.fetch_session(2026, meeting, session, force=True)

        # Sanity: confirm the injected exception actually reached run()'s
        # except-and-swallow path, so a failure above is attributable to the
        # caller's missing check -- not to the injection failing to fire.
        self.assertIn("pre", captured, "SessionPreProcessor was never constructed")
        self.assertTrue(
            captured["pre"].failed,
            "the injected exception did not reach run()'s except path -- "
            "this test is not exercising the bug it claims to"
        )


if __name__ == "__main__":
    unittest.main()
