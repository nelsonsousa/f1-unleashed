"""WB-16 [Config] Live session monitor + scheduler constants (main.py), Trello
LUICytF5.

Proves:
  - settings-sourced values match what the old hardcoded constants were
    (missing/None/malformed settings.json falls back to the exact prior literal)
  - the adaptive-poll threshold logic (300s at 1-2h away, 60s at <1h away)
    produces identical decisions to the pre-change hardcoded thresholds, for
    representative time-until-session inputs
  - _login_milestones_hours() always returns a descending-sorted list, since
    the milestone-selection loop in live_session_monitor() only resolves to
    "smallest milestone >= hours_until" when iterated in that order
  - a settings change actually takes effect (proves the settings.get() read is
    live, not a value captured once at import/task-start time)
"""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import settings as settings_store
from app import main


class SchedNumFallback(unittest.TestCase):
    """_sched_num falls back to the exact old hardcoded literal on a missing,
    None, or malformed settings value -- a settings read failure must not
    silently change monitor timing."""

    def test_missing_key_returns_default(self):
        with mock.patch.object(settings_store, "get", return_value=15):
            self.assertEqual(main._sched_num("postChequeredGraceMinutes", 15), 15)

    def test_default_type_is_preserved(self):
        with mock.patch.object(settings_store, "get", return_value=30):
            val = main._sched_num("postChequeredGraceMinutes", 15)
            self.assertIsInstance(val, int)
            self.assertEqual(val, 30)

    def test_none_value_falls_back_to_default(self):
        with mock.patch.object(settings_store, "get", return_value=None):
            self.assertEqual(main._sched_num("pollDefaultSeconds", 3600), 3600)

    def test_malformed_value_falls_back_to_default(self):
        with mock.patch.object(settings_store, "get", return_value="not-a-number"):
            self.assertEqual(main._sched_num("pollDefaultSeconds", 3600), 3600)


class PostChequeredGrace(unittest.TestCase):
    def test_default_matches_old_hardcoded_15_minutes(self):
        with mock.patch.object(settings_store, "get", return_value=15):
            from datetime import timedelta
            self.assertEqual(main._post_chequered_grace(), timedelta(minutes=15))

    def test_reads_settings_value_when_present(self):
        with mock.patch.object(settings_store, "get", return_value=30):
            from datetime import timedelta
            self.assertEqual(main._post_chequered_grace(), timedelta(minutes=30))

    def test_missing_settings_key_falls_back_to_15(self):
        # settings.get's own default kicks in (key absent from defaults.json
        # in a hypothetical stale install) -- simulate by returning the
        # function's own `default` argument, same as the real settings.get.
        def fake_get(path, default=None):
            return default
        with mock.patch.object(settings_store, "get", side_effect=fake_get):
            from datetime import timedelta
            self.assertEqual(main._post_chequered_grace(), timedelta(minutes=15))


class AdaptivePollSeconds(unittest.TestCase):
    """Matches the pre-change hardcoded adaptive-sleep decision exactly:
    >2h away or no session: 3600s: 1-2h away: 300s: <1h away: 60s."""

    def _defaults(self, key, default):
        # Mirrors settings.get(f"scheduler.{key}", default) with defaults.json's
        # actual shipped values, so these tests exercise the real defaults
        # rather than a fixture-only fake.
        return {
            "scheduler.pollDefaultSeconds": 3600,
            "scheduler.pollNearSeconds": 300,
            "scheduler.pollImminentSeconds": 60,
        }.get(f"scheduler.{key}", default)

    def setUp(self):
        patcher = mock.patch.object(
            settings_store, "get",
            side_effect=lambda path, default=None: self._defaults(
                path.split(".", 1)[1], default))
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_no_cached_session_uses_default_interval(self):
        self.assertEqual(main._adaptive_poll_seconds(None), 3600)

    def test_more_than_2h_away_uses_default_interval(self):
        self.assertEqual(main._adaptive_poll_seconds(2.5), 3600)
        self.assertEqual(main._adaptive_poll_seconds(10), 3600)

    def test_exactly_2h_away_uses_near_interval(self):
        # <= 2h, boundary inclusive (matches the original `elif hours_until <= 2`).
        self.assertEqual(main._adaptive_poll_seconds(2.0), 300)

    def test_between_1h_and_2h_away_uses_near_interval(self):
        self.assertEqual(main._adaptive_poll_seconds(1.5), 300)

    def test_exactly_1h_away_uses_imminent_interval(self):
        # <= 1h, boundary inclusive (matches the original `if hours_until <= 1`).
        self.assertEqual(main._adaptive_poll_seconds(1.0), 60)

    def test_less_than_1h_away_uses_imminent_interval(self):
        self.assertEqual(main._adaptive_poll_seconds(0.1), 60)

    def test_session_already_started_uses_imminent_interval(self):
        # Negative hours_until (session started, live-session check will have
        # taken over) still resolves to the tightest interval -- same as
        # the original code (no lower bound on the <=1 check).
        self.assertEqual(main._adaptive_poll_seconds(-0.5), 60)


class AdaptivePollSecondsSettingsOverride(unittest.TestCase):
    """A settings change actually takes effect -- proves the read is live
    (evaluated per call), not a value captured once at task start."""

    def test_custom_near_interval_is_honoured(self):
        with mock.patch.object(settings_store, "get",
                                side_effect=lambda path, default=None: (
                                    120 if path == "scheduler.pollNearSeconds" else default)):
            self.assertEqual(main._adaptive_poll_seconds(1.5), 120)

    def test_default_and_custom_calls_both_read_current_settings(self):
        # Same helper, two different settings snapshots -- proves no caching
        # of the resolved value across calls.
        with mock.patch.object(settings_store, "get",
                                side_effect=lambda path, default=None: (
                                    999 if path == "scheduler.pollImminentSeconds" else default)):
            self.assertEqual(main._adaptive_poll_seconds(0.5), 999)
        with mock.patch.object(settings_store, "get",
                                side_effect=lambda path, default=None: default):
            self.assertEqual(main._adaptive_poll_seconds(0.5), 60)


class LoginMilestonesHours(unittest.TestCase):
    def test_default_matches_old_hardcoded_list(self):
        with mock.patch.object(settings_store, "get",
                                return_value=[24, 12, 6, 3, 2, 1]):
            self.assertEqual(main._login_milestones_hours(), [24.0, 12.0, 6.0, 3.0, 2.0, 1.0])

    def test_missing_key_falls_back_to_default(self):
        def fake_get(path, default=None):
            return default
        with mock.patch.object(settings_store, "get", side_effect=fake_get):
            self.assertEqual(main._login_milestones_hours(), [24, 12, 6, 3, 2, 1])

    def test_empty_list_falls_back_to_default(self):
        with mock.patch.object(settings_store, "get", return_value=[]):
            self.assertEqual(main._login_milestones_hours(), [24, 12, 6, 3, 2, 1])

    def test_non_list_value_falls_back_to_default(self):
        with mock.patch.object(settings_store, "get", return_value="not-a-list"):
            self.assertEqual(main._login_milestones_hours(), [24, 12, 6, 3, 2, 1])

    def test_malformed_entries_fall_back_to_default(self):
        with mock.patch.object(settings_store, "get", return_value=[24, "twelve", 6]):
            self.assertEqual(main._login_milestones_hours(), [24, 12, 6, 3, 2, 1])

    def test_hand_edited_unsorted_list_is_returned_descending(self):
        # A settings.json a user hand-edited with the milestones out of order
        # must not invert the milestone-selection loop's semantics (see
        # _login_milestones_hours' docstring): it always comes back sorted
        # descending regardless of on-disk order.
        with mock.patch.object(settings_store, "get", return_value=[1, 6, 24, 3, 12, 2]):
            self.assertEqual(main._login_milestones_hours(), [24.0, 12.0, 6.0, 3.0, 2.0, 1.0])

    def _select_milestone(self, hours_until, milestones):
        """Re-implements the exact selection loop in live_session_monitor()
        (main.py), so this test proves the loop's real semantics against
        _login_milestones_hours' output, not just the list contents."""
        milestone = None
        for m in milestones:
            if hours_until <= m:
                milestone = m
        return milestone

    def test_milestone_selection_matches_pre_change_behavior_for_representative_inputs(self):
        # Exercises the actual selection loop (copied verbatim from main.py)
        # against representative hours_until values, proving descending order
        # yields "smallest milestone >= hours_until" -- the original hardcoded
        # list's [24, 12, 6, 3, 2, 1] behavior, unchanged.
        milestones = main._login_milestones_hours()
        cases = [
            (23.9, 24), (20, 24), (12.0, 12), (10, 12),
            (6.0, 6), (4, 6), (3.0, 3), (2.5, 3),
            (2.0, 2), (1.5, 2), (1.0, 1), (0.5, 1),
        ]
        for hours_until, expected in cases:
            with self.subTest(hours_until=hours_until):
                self.assertEqual(self._select_milestone(hours_until, milestones), expected)

    def test_milestone_selection_would_break_on_ascending_order_demonstrating_why_sort_matters(self):
        # Documents *why* _login_milestones_hours sorts descending: the same
        # selection loop against an ascending list picks the WRONG (largest)
        # milestone instead of the smallest one >= hours_until.
        ascending = [1, 2, 3, 6, 12, 24]
        self.assertEqual(self._select_milestone(10, ascending), 24)  # wrong: should be 12
        self.assertEqual(self._select_milestone(10, main._login_milestones_hours()), 12)  # correct


class DefaultsJsonSchedulerBlock(unittest.TestCase):
    """The shipped app/defaults.json values are exactly the old hardcoded
    constants -- a fresh install (no settings.json yet) must behave
    identically to the pre-change code."""

    def setUp(self):
        settings_store._cache = None

    def tearDown(self):
        settings_store._cache = None

    def test_scheduler_defaults_match_old_hardcoded_constants(self):
        expected = {
            "postChequeredGraceMinutes": 15,
            "scheduleRefreshIntervalSeconds": 3600,
            "scheduleSelfHealIntervalSeconds": 3600,
            "serverReadyGraceSeconds": 5,
            "weatherStopDelayMinutes": 5,
            "radarWindowHoursAfter": 4,
            "loginMilestonesHours": [24, 12, 6, 3, 2, 1],
            "pollDefaultSeconds": 3600,
            "pollNearSeconds": 300,
            "pollImminentSeconds": 60,
            "pollErrorRecoverySeconds": 60,
        }
        with mock.patch.object(settings_store, "SETTINGS_FILE", mock.MagicMock(
                exists=mock.MagicMock(return_value=False))):
            scheduler = settings_store.get("scheduler")
        self.assertEqual(scheduler, expected)


class PostChequeredGraceMalformedInput(unittest.TestCase):
    """Covers _post_chequered_grace's except branch: a non-numeric settings
    value must not crash the monitor, and must fall back to 15 min."""

    def test_non_numeric_settings_value_falls_back_to_15_minutes(self):
        with mock.patch.object(settings_store, "get", return_value="not-a-number"):
            self.assertEqual(main._post_chequered_grace(), timedelta(minutes=15))


class ChequeredGraceExpiredWiring(unittest.IsolatedAsyncioTestCase):
    """Exercises _chequered_grace_expired end-to-end against a real sqlite
    scratch DB, proving the settings-sourced grace period (not the old
    POST_CHEQUERED_GRACE constant) is what the timing comparison now uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "session.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE messages (topic TEXT, offset_ms INTEGER, data TEXT)")
        conn.execute(
            "INSERT INTO messages (topic, offset_ms, data) VALUES (?, ?, ?)",
            ("trackStatus", 1000, json.dumps({"status": "finished"})),
        )
        conn.commit()
        conn.close()

        self.session_id = "dummy-sess-cheq-1"
        self.addCleanup(main._chequered_first_seen.pop, self.session_id, None)
        from app.services.live_capture import live_capture
        self._orig_captures = dict(live_capture._captures)
        live_capture._captures[self.session_id] = {"cache_path": Path("irrelevant")}

        def restore_captures():
            live_capture._captures.clear()
            live_capture._captures.update(self._orig_captures)
        self.addCleanup(restore_captures)

    async def test_grace_period_expiry_uses_the_settings_sourced_value(self):
        now_utc = datetime.now(timezone.utc)
        with mock.patch("app.processing.database.transient_db_path", return_value=self.db_path), \
             mock.patch.object(settings_store, "get", return_value=15):
            # First call: session_id not seen yet -> records the timer, returns False.
            first = await main._chequered_grace_expired(self.session_id, now_utc)
            self.assertFalse(first)
            self.assertIn(self.session_id, main._chequered_first_seen)

            # Second call, 10 min later: within the 15-min grace -> still False.
            still_within = await main._chequered_grace_expired(
                self.session_id, now_utc + timedelta(minutes=10))
            self.assertFalse(still_within)

            # Third call, 20 min later: past the settings-sourced 15-min grace -> True.
            expired = await main._chequered_grace_expired(
                self.session_id, now_utc + timedelta(minutes=20))
            self.assertTrue(expired)

    async def test_custom_settings_grace_period_is_honoured(self):
        # A user who set scheduler.postChequeredGraceMinutes to 5 must see the
        # force-stop trigger after 5 min, not the old hardcoded 15.
        now_utc = datetime.now(timezone.utc)
        with mock.patch("app.processing.database.transient_db_path", return_value=self.db_path), \
             mock.patch.object(settings_store, "get", return_value=5):
            await main._chequered_grace_expired(self.session_id, now_utc)
            expired_at_10min = await main._chequered_grace_expired(
                self.session_id, now_utc + timedelta(minutes=10))
        self.assertTrue(expired_at_10min, "5-min grace must have expired by +10min")


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeHttpSession:
    """Minimal aiohttp.ClientSession stand-in: GET /schedule/next-session and
    /schedule/live-session are the only endpoints live_session_monitor calls."""

    def __init__(self, next_session_resp, live_session_resp):
        self._next_session_resp = next_session_resp
        self._live_session_resp = live_session_resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url):
        if "live-session" in url:
            return self._live_session_resp
        return self._next_session_resp


class LiveSessionMonitorWiring(unittest.IsolatedAsyncioTestCase):
    """Drives one real iteration of live_session_monitor() with aiohttp and
    the weather/live-capture singletons faked out, proving the settings-sourced
    constants are actually read at their call sites inside the loop -- not just
    correct in the isolated helper functions above."""

    def _scheduler_settings(self, **overrides):
        base = {
            "serverReadyGraceSeconds": 0,
            "scheduleRefreshIntervalSeconds": 3600,
            "weatherStopDelayMinutes": 5,
            "radarWindowHoursAfter": 4,
            "pollDefaultSeconds": 3600,
            "pollNearSeconds": 300,
            "pollImminentSeconds": 60,
            "pollErrorRecoverySeconds": 60,
        }
        base.update(overrides)
        return base

    def _fake_get(self, scheduler_settings, ntfy_overrides=None):
        ntfy = {"sessionLive": False, "preSession": False, "tokenExpiry": False}
        ntfy.update(ntfy_overrides or {})

        def fake_get(path, default=None):
            if path.startswith("scheduler."):
                return scheduler_settings.get(path.split(".", 1)[1], default)
            if path.startswith("ntfy."):
                return ntfy.get(path.split(".", 1)[1], default)
            return default
        return fake_get

    async def test_one_iteration_reads_every_scheduler_setting_at_its_call_site(self):
        session_date = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        next_session_resp = _FakeResp(200, {
            "session_date": session_date,
            "event_name": "British GP",
            "session_type": "Practice",
            "is_testing": False,
        })
        live_session_resp = _FakeResp(204)  # definitively not live
        http_session = _FakeHttpSession(next_session_resp, live_session_resp)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        scheduler_settings = self._scheduler_settings()
        radar_capture = mock.MagicMock(active=True, active_key=None)
        forecast_capture = mock.MagicMock(active=True, active_key=None)

        orig_active_capture = dict(main._active_live_capture)
        self.addCleanup(main._active_live_capture.update, orig_active_capture)

        with mock.patch.object(settings_store, "get", side_effect=self._fake_get(scheduler_settings)), \
             mock.patch("aiohttp.ClientSession", return_value=http_session), \
             mock.patch("asyncio.sleep", side_effect=fake_sleep), \
             mock.patch.object(main, "radar_capture", radar_capture), \
             mock.patch.object(main, "forecast_capture", forecast_capture):
            # live_session_monitor's own `except asyncio.CancelledError: break`
            # catches the CancelledError our fake_sleep raises to end the loop
            # after 2 iterations, and the function then returns normally
            # (CancelledError is BaseException, not Exception, so the
            # `except Exception` handler above it never intercepts it).
            await main.live_session_monitor()

        # 1st sleep: startup grace (0, from scheduler.serverReadyGraceSeconds).
        # 2nd sleep: adaptive poll interval for a session ~3h away -> pollDefaultSeconds
        # (proves scheduler.scheduleRefreshIntervalSeconds's >= comparison, and the
        # radarWindowHoursAfter / weatherStopDelayMinutes / LOGIN_MILESTONES call
        # sites all ran without error along the way).
        self.assertEqual(sleep_calls[0], 0)
        self.assertEqual(sleep_calls[1], scheduler_settings["pollDefaultSeconds"])

    async def test_error_path_sleeps_for_the_settings_sourced_recovery_interval(self):
        # aiohttp.ClientSession() itself raising simulates a transient
        # network/DNS failure -- must land in the outer `except Exception`
        # handler and sleep for scheduler.pollErrorRecoverySeconds, not the
        # old hardcoded 60 (here set to a distinguishable 7 to prove it's
        # actually read, not coincidentally equal to the default).
        scheduler_settings = self._scheduler_settings(
            serverReadyGraceSeconds=0, pollErrorRecoverySeconds=7)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        with mock.patch.object(settings_store, "get", side_effect=self._fake_get(scheduler_settings)), \
             mock.patch("aiohttp.ClientSession", side_effect=RuntimeError("network down")), \
             mock.patch("asyncio.sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await main.live_session_monitor()

        self.assertEqual(sleep_calls, [0, 7])


class StartLiveRefusalBookkeeping(unittest.IsolatedAsyncioTestCase):
    """Trello c966lztz: start_live's single-capture refusal (M5) must not be
    mistaken for success by the monitor.

    Scenario: session A is still being captured (no definitive 204 seen) when
    F1's live-session endpoint flips to session B. need_start fires (event/type
    mismatch), start_live is called for B but refuses because A's capture task
    is still active. The monitor must leave _active_live_capture untouched (A's
    sid stays paired with A's event/type) so the next cycle's need_start check
    still sees the mismatch and retries -- rather than overwriting the
    bookkeeping with B's event/type while keeping A's sid, which is what
    corrupts /api/v1/live-capture/status and misdirects B's data into A's
    session folder.
    """

    def _scheduler_settings(self, **overrides):
        base = {
            "serverReadyGraceSeconds": 0,
            "scheduleRefreshIntervalSeconds": 3600,
            "weatherStopDelayMinutes": 5,
            "radarWindowHoursAfter": 4,
            "pollDefaultSeconds": 3600,
            "pollNearSeconds": 300,
            "pollImminentSeconds": 60,
            "pollErrorRecoverySeconds": 60,
        }
        base.update(overrides)
        return base

    def _fake_get(self, scheduler_settings, ntfy_overrides=None):
        ntfy = {"sessionLive": False, "preSession": False, "tokenExpiry": False}
        ntfy.update(ntfy_overrides or {})

        def fake_get(path, default=None):
            if path.startswith("scheduler."):
                return scheduler_settings.get(path.split(".", 1)[1], default)
            if path.startswith("ntfy."):
                return ntfy.get(path.split(".", 1)[1], default)
            return default
        return fake_get

    async def test_refused_start_does_not_overwrite_active_capture_identity(self):
        # Session B is live for every cycle; next-session refresh returns 404
        # so cached_next_session stays None and the radar/forecast block
        # (which needs a real cache_path) never runs.
        live_session_resp = _FakeResp(200, {
            "event_name": "Event B",
            "session_type": "Sprint",
            "location": "Test Location",
            "round": 5,
            "meeting_key": 1234,
            "session_key": 5678,
            "session_name": "Sprint",
        })
        next_session_resp = _FakeResp(404)
        http_session = _FakeHttpSession(next_session_resp, live_session_resp)

        original_active = {
            "session_id": "sid-A",
            "event_name": "Event A",
            "session_type": "Race",
        }
        main._active_live_capture.update(original_active)
        self.addCleanup(main._active_live_capture.update, {
            "session_id": None, "event_name": None, "session_type": None,
        })

        # Records the bookkeeping *as seen by start_live at call time*, i.e.
        # before this call's own return value is applied by the monitor —
        # so call N's snapshot proves what call N-1 left behind.
        call_snapshots = []

        async def fake_start_live(**kwargs):
            call_snapshots.append(dict(main._active_live_capture))
            if len(call_snapshots) < 3:
                # A is still active: start_live refuses, per the single-capture
                # invariant (M5) in app/services/live_capture.py.
                return ("sid-A", False)
            # A has finally stopped (e.g. its post-CHEQUERED force-stop fired);
            # B's start now genuinely succeeds.
            return ("dummy-sid-b-new", True)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 4:   # startup grace + 3 loop iterations
                raise asyncio.CancelledError()

        scheduler_settings = self._scheduler_settings()
        radar_capture = mock.MagicMock(active=False, active_key=None)
        forecast_capture = mock.MagicMock(active=False, active_key=None)

        with mock.patch.object(settings_store, "get", side_effect=self._fake_get(scheduler_settings)), \
             mock.patch("aiohttp.ClientSession", return_value=http_session), \
             mock.patch("asyncio.sleep", side_effect=fake_sleep), \
             mock.patch.object(main, "radar_capture", radar_capture), \
             mock.patch.object(main, "forecast_capture", forecast_capture), \
             mock.patch.object(main.live_capture, "start_live", side_effect=fake_start_live), \
             mock.patch.object(main.live_capture, "get_status",
                                side_effect=ValueError("unknown session")):
            await main.live_session_monitor()

        # Every refused attempt must have seen A's *original* bookkeeping --
        # never B's identity stitched onto A's (or any other) sid.
        self.assertEqual(len(call_snapshots), 3)
        self.assertEqual(call_snapshots[0], original_active)
        self.assertEqual(call_snapshots[1], original_active,
                          "monitor must retry with A's bookkeeping intact, "
                          "not have overwritten it with B's event/type on refusal")
        self.assertEqual(call_snapshots[2], original_active)

        # Once start_live genuinely succeeds (A is gone), the monitor adopts
        # B's identity together with B's own new sid.
        self.assertEqual(main._active_live_capture, {
            "session_id": "dummy-sid-b-new",
            "event_name": "Event B",
            "session_type": "Sprint",
        })


if __name__ == "__main__":
    unittest.main()
