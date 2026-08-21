"""RED GATE — Trello cards `Orts6BRn` ("Audio: choppy playback on fresh live
connect, buffer starvation") and `cJJUzyAj` ("Audio: not playing /
traffic-light yellow / buffers >3min"). Two red-gate passes live in this one
file, run at different times against different states of `session.py`:

PASS 1 (landed, commit `26b22d5` on this branch) — `LIVE_EDGE_MARGIN_S`
5.0 -> 15.0, `_seek()`'s redundant `-10.0` removed. AC1/AC3(stale)/AC4/AC5/AC6
below are that pass's surviving tests, now GREEN against the code on this
branch. AC7 (quiet-period simulation) was proven, in the same pass, to fail
STRUCTURALLY regardless of the margin's value — the freeze is bounded by
heartbeat cadence, not eliminated by a bigger margin. See
docs/artifacts/2026-08-20-073-backlog-regrouping-sprint-replan/scoping-Orts6BRn-cJJUzyAj.md.

PASS 2 (this red gate, NOT yet implemented) — the wall-clock-driven live
playhead redesign specified in full in
docs/artifacts/2026-08-20-073-backlog-regrouping-sprint-replan/scoping-Orts6BRn-wallclock-playhead.md
(§4, AC-W1 through AC-W10). For a LIVE session with a genuinely healthy
connection (the existing `data_healthy` signal), the ceiling becomes
`min(wall_clock_offset_ms, audio_ms - AUDIO_SKEW_MARGIN_S*1000)` instead of
being gated by the last-processed message's timestamp. Replay and
live-but-stale sessions are UNCHANGED (still the old margin-based fallback).

Per the design doc's own explicit instruction, two PASS-1 tests encoded
behaviour that PASS 2 makes wrong on purpose and have been REPLACED, not
left running alongside the new tests:
  - old `LiveEdgeMsAppliesNewMargin_DataOnlyBranch` (AC2: "fresh connect
    lands raw_edge - 15000ms") -> replaced by `WallClockDrivenCeiling_NoAudio`
    (AC-W1/AC-W6: fresh connect lands at raw wall-clock time).
  - old `QuietPeriodHeartbeatCadenceDoesNotFreezeTheClock` (AC7: "frozen
    time stays under a 2.0s budget") -> rewritten in place, same class name,
    to assert ZERO frozen ticks (AC-W4) -- the old budget-based threshold was
    calibrated to a margin design that could only ever bound the freeze,
    never eliminate it, and would pass post-fix without proving the new
    mechanism (full decoupling from the data edge) is actually in effect.

A THIRD test not named by number in the design doc's explicit replacement
list was ALSO replaced for the same reason, on this suite's own judgement:
old `LiveEdgeMsAppliesNewMargin_BothHealthyBranch` exercised "live, data AND
audio both healthy" -- exactly the scenario AC-W2 says now takes the
wall-clock branch, not the old margin-of-minimum formula. Leaving its old
assertion in place would have been the identical silent-regression-trap the
design doc explicitly calls out for AC2 (scoping-Orts6BRn-wallclock-playhead.md
§8, AC-W6), just unnamed. Replaced by `WallClockDrivenCeiling_AudioPresent`
(AC-W2). Flagged here, and in test-plan-Orts6BRn.md, for the human/reviewer
to confirm this extrapolation was correct.

Every PASS-2 test below is written from the design doc's own pseudocode
(§4.3), NOT by reading a fix that has been implemented -- there is no
implementation yet: `app/processing/session.py` and `app/processing/clock.py`
are unmodified from `test` @ `26b22d5`. Wall-clock-simulation tests patch
`app.processing.session.datetime` (the `_wall_clock_offset_ms()` method this
red gate targets does not exist yet, so on the CURRENT code that patch is
inert -- `_live_edge_ms()` never reads `datetime.now()` today -- and the
result comes purely from the OLD formula, which is exactly what produces a
clean, legible mismatch against the NEW formula's expected value).
"""
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.processing.clock import ClockState
from app.processing.session import SessionEngine, LIVE_EDGE_MARGIN_S

# AUDIO_SKEW_MARGIN_S does not exist on `app.processing.session` yet (it is
# part of the PASS-2 design, scoping-Orts6BRn-wallclock-playhead.md §4.1) --
# importing it here would turn every test in this file into a collection
# error (ImportError), not a red-gate failure for the right reason. Pinned
# locally instead; AC-W9 below separately guards that the *unrelated*
# existing constants (DATA_EDGE_STALE_S, AUDIO_EDGE_STALE_S) are untouched.
# Once AUDIO_SKEW_MARGIN_S lands, replace this with a real import + an AC1-style
# pin test for it.
_EXPECTED_AUDIO_SKEW_MARGIN_MS = 5_000


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class _FakeConn:
    """Stands in for `SessionDatabase._conn` -- only the one query
    `_data_edge_ms` actually issues (`SELECT MAX(offset_ms) FROM messages`)
    is exercised; `execute()` ignores the SQL text and always returns self so
    `.fetchone()` can hand back whatever `max_offset_ms` currently is."""

    def __init__(self, holder):
        self._holder = holder

    def execute(self, _sql, *_args):
        return self

    def fetchone(self):
        if self._holder["max_offset_ms"] is None:
            return None
        return (self._holder["max_offset_ms"],)

    def fetchall(self):
        # add_client()'s scrubber-events query (`WHERE topic = 'event'`) --
        # no test in this file needs non-empty events, so an empty result set
        # exercises the query path without asserting on its contents.
        return []


class _FakeDB:
    """Minimal double for `SessionDatabase`. Only implements what
    `_live_edge_ms()` / `_seek()` actually call on it
    (`_conn` for `_data_edge_ms`, `get_state_at`, `get_max_rowid`). Every
    other `SessionDatabase` method `_seek()`'s downstream helpers touch
    (`_send_restore_extras`) is deliberately absent -- those calls are each
    individually wrapped in `try/except Exception` in production code, so a
    missing attribute is swallowed exactly as a genuinely-empty table would
    be, and is not part of what this suite is proving."""

    def __init__(self, max_offset_ms):
        self._holder = {"max_offset_ms": max_offset_ms}
        self._conn = _FakeConn(self._holder)

    def set_max_offset_ms(self, value):
        self._holder["max_offset_ms"] = value

    def get_state_at(self, offset_ms):
        return {}

    def get_max_rowid(self):
        return 0


def _make_engine(live: bool, max_offset_ms=None) -> SessionEngine:
    """A `SessionEngine` built WITHOUT calling `.start()` (no filesystem I/O,
    no preprocessing, no WebSocket) -- exactly the object shape
    `_live_edge_ms()` / `_seek()` need: `_db`, `_clock`, `_start_time`,
    `_live`, `_preprocess_done`, `_duration`. `_session_path` deliberately
    points at a nonexistent directory so `_audio_edge_offset()`'s
    `pdt_map.jsonl` check always resolves to "no audio" unless a test
    monkeypatches `_audio_edge_offset` directly."""
    engine = SessionEngine(
        session_path=Path("/nonexistent/orts6brn-test-session"),
        session_name="orts6brn-test",
        live=live,
    )
    engine._start_time = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    if max_offset_ms is not None:
        engine._db = _FakeDB(max_offset_ms)
    return engine


def _patch_wall_clock_now(start: datetime, elapsed_ms_fn):
    """Patches `app.processing.session.datetime` so that, once
    `_wall_clock_offset_ms()` exists (PASS 2, not yet implemented),
    `datetime.now(timezone.utc)` resolves to `start + elapsed_ms_fn()`
    milliseconds. `elapsed_ms_fn` is called fresh on every `.now()` call so
    it can track a mutable "simulated wall clock" across ticks (see
    `WallClock...` tests / AC-W4 / AC-W5).

    Inert against the CURRENT (unfixed) code: `_live_edge_ms()` does not
    call `datetime.now()` anywhere today, so this patch changes nothing
    about the OLD-formula result it currently returns -- it exists so the
    same test, unmodified, exercises the real timing the POST-FIX code
    will read once `_wall_clock_offset_ms()` lands."""
    mock_dt = mock.MagicMock(wraps=datetime)
    mock_dt.now.side_effect = lambda *a, **kw: start + timedelta(
        milliseconds=elapsed_ms_fn())
    return mock.patch("app.processing.session.datetime", mock_dt)


# ---------------------------------------------------------------------------
# AC1 -- LIVE_EDGE_MARGIN_S pinned at the (PASS-1-fixed) value. Still GREEN:
# PASS 2 keeps the constant, it just narrows which branch reads it.
# ---------------------------------------------------------------------------

class LiveEdgeMarginConstantValue(unittest.TestCase):
    def test_live_edge_margin_s_is_fifteen_seconds(self):
        """Pinned at 15.0 by PASS 1 (26b22d5). PASS 2 does not touch this
        constant's value (scoping-Orts6BRn-wallclock-playhead.md §5) --
        expected to stay GREEN through both passes."""
        self.assertEqual(
            LIVE_EDGE_MARGIN_S, 15.0,
            f"LIVE_EDGE_MARGIN_S is {LIVE_EDGE_MARGIN_S}, expected 15.0")


# ---------------------------------------------------------------------------
# AC-W1 / AC-W6 -- Live + healthy + NO audio: ceiling tracks wall-clock time
# exactly, not the data-edge-minus-margin formula. This is the direct
# replacement for the old (PASS-1) `LiveEdgeMsAppliesNewMargin_DataOnlyBranch`
# class, whose AC2 assertion ("raw edge - 15000ms") is precisely the behaviour
# this redesign removes for the healthy-live case
# (scoping-Orts6BRn-wallclock-playhead.md §8, AC-W6: "test-engineer: replace
# ... don't add this alongside it").
#
# One test proves both design ACs: AC-W1's own framing ("exercise
# _live_edge_ms() directly with data_ms present/healthy/no audio") and
# AC-W6's framing ("add_client()'s first-connect seek lands at wall-clock
# time") are the same assertion at the same level -- add_client() applies NO
# further transformation to _live_edge_ms()'s return value
# (session.py:537-538: `edge_ms = self._live_edge_ms()` feeds
# `self._clock.seek_to_offset(self._duration)` directly), the same
# test-level-scoping call the PASS-1 suite already made for AC2 and that
# test-plan-Orts6BRn.md documents explicitly.
# ---------------------------------------------------------------------------

class WallClockDrivenCeiling_NoAudio(unittest.TestCase):
    """Live session, healthy connection, no audio edge available (no
    pdt_map.jsonl) -- the new wall-clock branch of `_live_edge_ms`
    (scoping-Orts6BRn-wallclock-playhead.md §4.3, `if self._live and
    data_healthy: ... capped = wall_ms`)."""

    def test_live_edge_ms_equals_raw_wall_clock_elapsed_time(self):
        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        # data_ms is deliberately set far ABOVE the simulated wall-clock
        # elapsed time -- if _live_edge_ms() is still driven off data_ms
        # (the OLD formula), the result will be nowhere near wall_ms, giving
        # an unambiguous, large mismatch rather than a coincidental near-miss.
        elapsed_s = 42.7
        wall_ms = int(elapsed_s * 1000)
        data_ms = 999_000  # far above wall_ms; must NOT drive the result
        engine = _make_engine(live=True, max_offset_ms=data_ms)
        engine._start_time = start
        # Healthy: no staleness set up, matches the existing suite's default
        # (getattr(..., now_mono) makes the first call always "just advanced").

        with _patch_wall_clock_now(start, lambda: wall_ms):
            ceiling_ms = engine._live_edge_ms()

        self.assertEqual(
            ceiling_ms, wall_ms,
            f"_live_edge_ms() returned {ceiling_ms}, expected {wall_ms} "
            f"(raw wall-clock elapsed time, {elapsed_s}s after session start) "
            f"-- got a value derived from data_ms ({data_ms}) instead, i.e. "
            f"the wall-clock branch does not exist yet "
            f"(scoping-Orts6BRn-wallclock-playhead.md AC-W1/AC-W6)")


class WallClockOffsetMsNoStartTime(unittest.TestCase):
    """Branch-coverage guard, not a named AC: `_wall_clock_offset_ms()`'s own
    `if not self._start_time: return 0` guard (scoping-Orts6BRn-wallclock-playhead.md
    §4.2) is never exercised by any of the AC-W* tests above -- every one of
    them sets `_start_time` via `_make_engine`/explicit assignment before
    calling `_live_edge_ms()`. `app/processing/session.py` is a Critical Path
    (CLAUDE.local.md) requiring 100% branch coverage on changed lines, so this
    guard's own branch needs its own direct test rather than relying on
    incidental coverage from the AC-W* suite."""

    def test_returns_zero_when_start_time_not_set(self):
        engine = _make_engine(live=True, max_offset_ms=None)
        engine._start_time = None

        self.assertEqual(engine._wall_clock_offset_ms(), 0)


# ---------------------------------------------------------------------------
# AC-W2 -- Live + healthy + audio present: ceiling is
# min(wall_ms, audio_ms - AUDIO_SKEW_MARGIN_S*1000), two sub-cases. Direct
# replacement for the old (PASS-1) `LiveEdgeMsAppliesNewMargin_BothHealthyBranch`
# class -- see this file's module docstring for why that replacement was made
# even though not named by number in the design doc's explicit list.
# ---------------------------------------------------------------------------

class WallClockDrivenCeiling_AudioPresent(unittest.TestCase):
    """Live session, healthy connection, audio edge present (monkeypatched
    `_audio_edge_offset`, matching the PASS-1 suite's own fixture
    convention -- the branch under test is the wall-clock/audio-skew
    arithmetic, not PDT-file parsing)."""

    def test_wall_clock_binds_when_audio_is_comfortably_ahead(self):
        """(a) wall-clock is the binding constraint."""
        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        elapsed_s = 10.0
        wall_ms = int(elapsed_s * 1000)
        audio_edge_s = 300.0  # far ahead of wall_ms -- audio must NOT bind
        engine = _make_engine(live=True, max_offset_ms=999_000)
        engine._start_time = start
        engine._audio_edge_offset = lambda: audio_edge_s

        with _patch_wall_clock_now(start, lambda: wall_ms):
            ceiling_ms = engine._live_edge_ms()

        self.assertEqual(
            ceiling_ms, wall_ms,
            f"_live_edge_ms() returned {ceiling_ms}, expected {wall_ms} "
            f"(wall-clock elapsed) with audio comfortably ahead "
            f"(audio_ms=300000, skew ceiling would be "
            f"{300_000 - _EXPECTED_AUDIO_SKEW_MARGIN_MS}) "
            f"(scoping-Orts6BRn-wallclock-playhead.md AC-W2a)")

    def test_audio_binds_when_audio_capture_is_lagging(self):
        """(b) audio (minus its skew margin) is the binding constraint."""
        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        elapsed_s = 120.0  # wall-clock has run far ahead
        wall_ms = int(elapsed_s * 1000)
        audio_edge_s = 20.0  # audio capture lagging well behind wall-clock
        engine = _make_engine(live=True, max_offset_ms=999_000)
        engine._start_time = start
        engine._audio_edge_offset = lambda: audio_edge_s

        with _patch_wall_clock_now(start, lambda: wall_ms):
            ceiling_ms = engine._live_edge_ms()

        expected_ms = int(audio_edge_s * 1000) - _EXPECTED_AUDIO_SKEW_MARGIN_MS
        self.assertEqual(
            ceiling_ms, expected_ms,
            f"_live_edge_ms() returned {ceiling_ms}, expected {expected_ms} "
            f"(audio_ms=20000 - {_EXPECTED_AUDIO_SKEW_MARGIN_MS}ms skew margin) "
            f"with wall-clock far ahead (wall_ms={wall_ms}) "
            f"(scoping-Orts6BRn-wallclock-playhead.md AC-W2b)")


# ---------------------------------------------------------------------------
# AC-W3 -- Live + STALE data (no advance for > DATA_EDGE_STALE_S): unchanged
# fallback. This is the PASS-1 `LiveEdgeMsAppliesNewMargin_AudioOnlyBranch`
# test, kept verbatim per the design doc's own instruction ("reuse the
# existing AC3 fixtures ... assert the exact same numeric result as today's
# shipped formula") -- proof the fallback branch is untouched, not merely
# close. Expected GREEN both before and after PASS 2 (this scenario never
# reaches the new wall-clock branch: self._live=True but data_healthy=False).
# ---------------------------------------------------------------------------

class LiveEdgeMsAppliesNewMargin_AudioOnlyBranch(unittest.TestCase):
    """Live session, data edge stale (feed stalled) -- the 'audio-only'
    fallback branch, unchanged by PASS 2 (scoping-Orts6BRn-wallclock-playhead.md
    AC-W3)."""

    def test_live_edge_ms_is_fifteen_seconds_below_audio_edge_when_data_stale(self):
        audio_edge_s = 300.0
        engine = _make_engine(live=True, max_offset_ms=50_000)
        engine._audio_edge_offset = lambda: audio_edge_s
        # Force data_healthy=False: last advance was DATA_EDGE_STALE_S+ ago.
        engine._last_data_ms = 50_000
        engine._last_data_advance = time.monotonic() - 31.0

        ceiling_ms = engine._live_edge_ms()

        expected_ms = int(audio_edge_s * 1000) - 15_000
        self.assertEqual(
            ceiling_ms, expected_ms,
            f"_live_edge_ms() (audio-only/data-stale fallback branch) "
            f"returned {ceiling_ms}, expected {expected_ms} -- this branch "
            f"must stay byte-for-byte unchanged by the wall-clock redesign "
            f"(scoping-Orts6BRn-wallclock-playhead.md AC-W3)")


# ---------------------------------------------------------------------------
# AC-W5 -- the stale-connection fallback actually engages MID-SESSION (the
# safety valve is real, not theoretical): data advances normally (wall-clock
# tracking), then stops entirely; the ceiling must stop climbing and freeze
# at the fallback value, and a _playback_loop-style clamp must snap the
# clock down to it.
# ---------------------------------------------------------------------------

class StaleConnectionFallbackEngagesMidSession(unittest.TestCase):
    """AC-W5. Uses subTest so all three assertions (pre-stale tracking,
    post-stale freeze, clamp-down) are evaluated and individually reported
    even though this suite is expected to fail before PASS 2 ships --
    distinguishing "genuinely red" sub-assertions (pre-stale wall-clock
    tracking, which does not exist yet) from ones that may already pass by
    coincidence (the post-stale fallback IS today's only formula for a live
    session, so it was already exercising something close to "frozen at the
    last confirmed position" even before this redesign)."""

    def test_ceiling_tracks_then_freezes_when_data_goes_stale(self):
        from app.processing.clock import PlaybackClock

        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(live=True, max_offset_ms=0)
        engine._clock = PlaybackClock(start)

        fake_mono = [1_000_000.0]
        fake_elapsed_s = [0.0]

        def fake_monotonic():
            return fake_mono[0]

        with mock.patch("time.monotonic", side_effect=fake_monotonic), \
                _patch_wall_clock_now(start, lambda: fake_elapsed_s[0] * 1000):
            # Phase 1 (0-10s): data advances every ~1s, keeping data_healthy
            # True and wall-clock tracking active.
            for i in range(1, 11):
                fake_mono[0] += 1.0
                fake_elapsed_s[0] += 1.0
                engine._db.set_max_offset_ms(int(fake_elapsed_s[0] * 1000))
                engine._clock.tick()

            with self.subTest("pre-stale: ceiling tracks wall-clock"):
                pre_stale_ceiling_ms = engine._live_edge_ms()
                expected_pre_ms = int(fake_elapsed_s[0] * 1000)
                self.assertEqual(
                    pre_stale_ceiling_ms, expected_pre_ms,
                    f"_live_edge_ms() returned {pre_stale_ceiling_ms} while "
                    f"data was healthy, expected {expected_pre_ms} "
                    f"(raw wall-clock elapsed) -- the wall-clock branch "
                    f"does not exist yet (scoping-Orts6BRn-wallclock-playhead.md AC-W5a)")

            frozen_data_ms = int(fake_elapsed_s[0] * 1000)  # last real advance

            # Phase 2: data STOPS advancing entirely (feed dead) while
            # wall-clock time keeps moving underneath it, for
            # DATA_EDGE_STALE_S (30.0s) + a margin.
            for i in range(35):
                fake_mono[0] += 1.0
                fake_elapsed_s[0] += 1.0
                # engine._db.set_max_offset_ms(...) NOT called -- data is frozen.
                engine._clock.tick()

            with self.subTest("post-stale: ceiling freezes at fallback value"):
                post_stale_ceiling_ms = engine._live_edge_ms()
                expected_frozen_ms = max(0, frozen_data_ms - int(LIVE_EDGE_MARGIN_S * 1000))
                self.assertEqual(
                    post_stale_ceiling_ms, expected_frozen_ms,
                    f"_live_edge_ms() returned {post_stale_ceiling_ms} once "
                    f"data went stale, expected the frozen fallback "
                    f"{expected_frozen_ms} (frozen data edge {frozen_data_ms} "
                    f"- LIVE_EDGE_MARGIN_S) "
                    f"(scoping-Orts6BRn-wallclock-playhead.md AC-W5b)")

            with self.subTest("clamp: clock offset snaps down to the frozen ceiling"):
                # Mirrors _playback_loop's own clamp (session.py:1199-1201).
                ceiling_ms = engine._live_edge_ms()
                if ceiling_ms is not None and engine._clock.offset_seconds * 1000 > ceiling_ms:
                    engine._clock.seek_to_offset(ceiling_ms / 1000.0)
                self.assertLessEqual(
                    engine._clock.offset_seconds * 1000, ceiling_ms + 1e-6,
                    f"Clock offset {engine._clock.offset_seconds * 1000}ms was "
                    f"not clamped down to the frozen ceiling {ceiling_ms}ms "
                    f"(scoping-Orts6BRn-wallclock-playhead.md AC-W5c)")


# ---------------------------------------------------------------------------
# AC4 -- no double-buffering: an explicit "go live" seek must NOT also
# subtract _seek()'s own -10.0 on top of the baked-in margin. Unchanged by
# PASS 2 -- this test passes a fixed synthetic ceiling_offset_s directly to
# _seek(); it never calls _live_edge_ms() and is orthogonal to how the
# ceiling itself gets computed. Still GREEN.
# ---------------------------------------------------------------------------

class SeekDoesNotDoubleBufferLiveEdge(unittest.IsolatedAsyncioTestCase):
    """AC4. `seekLive()` -> `_seek(ceiling)` where `ceiling` is the client's
    last-known live edge (`messageBus.duration`). Lands the clock EXACTLY on
    `ceiling` since PASS 1 (26b22d5) removed `_seek()`'s redundant -10.0."""

    async def test_seek_to_live_ceiling_lands_exactly_on_ceiling_not_ten_seconds_early(self):
        from app.processing.clock import PlaybackClock

        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(live=True, max_offset_ms=200_000)
        engine._clock = PlaybackClock(start)
        engine._duration = 3600.0  # generous seekable ceiling for this test
        ceiling_offset_s = 185.0  # e.g. messageBus.duration at seek time

        await engine._seek(ceiling_offset_s)

        self.assertAlmostEqual(
            engine._clock.offset_seconds, ceiling_offset_s, places=3,
            msg=(
                f"_seek({ceiling_offset_s}) landed the clock at "
                f"{engine._clock.offset_seconds}s, expected exactly "
                f"{ceiling_offset_s}s"
            ))


# ---------------------------------------------------------------------------
# AC-W7 -- Explicit "go live" seek lands at the SAME value as a fresh connect
# (AC-W1/AC-W6): single code path (_live_edge_ms()) serves both add_client()
# and _seek()'s seekLive() caller, so proving convergence is one test:
# compute the ceiling via _live_edge_ms() (as a caller would), then seek to
# it via _seek() (mirroring the existing AC4 fixture pattern), and confirm
# the clock lands at the wall-clock value.
# ---------------------------------------------------------------------------

class ExplicitGoLiveSeekConvergesWithFreshConnectLanding(unittest.IsolatedAsyncioTestCase):
    """AC-W7."""

    async def test_seek_live_lands_at_wall_clock_value_same_as_fresh_connect(self):
        from app.processing.clock import PlaybackClock

        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        elapsed_s = 63.25
        wall_ms = int(elapsed_s * 1000)
        engine = _make_engine(live=True, max_offset_ms=999_000)
        engine._start_time = start
        engine._clock = PlaybackClock(start)
        engine._duration = 3600.0  # generous seekable ceiling

        with _patch_wall_clock_now(start, lambda: wall_ms):
            ceiling_ms = engine._live_edge_ms()  # what seekLive()'s caller supplies
            await engine._seek(ceiling_ms / 1000.0)

        self.assertAlmostEqual(
            engine._clock.offset_seconds, elapsed_s, places=3,
            msg=(
                f"Explicit go-live seek landed at {engine._clock.offset_seconds}s, "
                f"expected {elapsed_s}s (raw wall-clock elapsed), matching "
                f"the fresh-connect landing point "
                f"(scoping-Orts6BRn-wallclock-playhead.md AC-W7)"
            ))


# ---------------------------------------------------------------------------
# AC5 -- ordinary history seeks land exactly where requested. Unchanged by
# PASS 2 -- unrelated to _live_edge_ms(). Still GREEN.
# ---------------------------------------------------------------------------

class OrdinaryHistorySeekLandsExactlyOnRequestedOffset(unittest.IsolatedAsyncioTestCase):
    """AC5. A seek to offset=100 in a 3600s-duration session lands at
    exactly 100."""

    async def test_seek_to_offset_100_lands_at_100_not_90(self):
        from app.processing.clock import PlaybackClock

        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(live=False, max_offset_ms=3_600_000)
        engine._clock = PlaybackClock(start)
        engine._duration = 3600.0

        await engine._seek(100.0)

        self.assertAlmostEqual(
            engine._clock.offset_seconds, 100.0, places=3,
            msg=(
                f"_seek(100.0) landed the clock at {engine._clock.offset_seconds}s, "
                f"expected exactly 100.0s"
            ))


# ---------------------------------------------------------------------------
# AC6 -- Completed-replay `_live_edge_ms()` branch is untouched by either
# PASS. NOT A RED-GATE TEST -- a scope-guard / non-regression control,
# expected to PASS before and after both fixes. Per
# scoping-Orts6BRn-wallclock-playhead.md §2/§4.3: `not self._live and
# self._preprocess_done.is_set()` returns `data_ms` unmodified and is the
# FIRST branch checked, before either PASS's logic is ever reached.
# ---------------------------------------------------------------------------

class CompletedReplayLiveEdgeIsUnaffectedByMargin(unittest.TestCase):
    def test_completed_replay_returns_raw_data_edge_with_no_margin_applied(self):
        raw_edge_ms = 3_600_000
        engine = _make_engine(live=False, max_offset_ms=raw_edge_ms)
        engine._preprocess_done.set()

        ceiling_ms = engine._live_edge_ms()

        self.assertEqual(
            ceiling_ms, raw_edge_ms,
            "Completed-replay _live_edge_ms() should return the raw data "
            "edge unmodified regardless of LIVE_EDGE_MARGIN_S or the "
            "wall-clock redesign")


# ---------------------------------------------------------------------------
# AC-W8 -- the STILL-BUILDING replay branch is also provably untouched by
# PASS 2 -- a real gap in the PASS-1 suite (AC6 only covered COMPLETED
# replay), and the specific claim scoping-Orts6BRn-wallclock-playhead.md §7
# rests on ("the build races ahead of wall-clock, so this ceiling formula
# is essentially never binding in practice -- but only true because this
# branch is untouched by the redesign, self._live=False throughout").
# ---------------------------------------------------------------------------

class StillBuildingReplayLiveEdgeIsUnaffectedByWallClock(unittest.TestCase):
    """AC-W8."""

    def test_still_building_replay_uses_old_margin_formula_unchanged(self):
        raw_edge_ms = 400_000
        engine = _make_engine(live=False, max_offset_ms=raw_edge_ms)
        # _preprocess_done deliberately left UNSET: still building.

        ceiling_ms = engine._live_edge_ms()

        expected_ms = raw_edge_ms - int(LIVE_EDGE_MARGIN_S * 1000)
        self.assertEqual(
            ceiling_ms, expected_ms,
            f"_live_edge_ms() (still-building replay) returned {ceiling_ms}, "
            f"expected {expected_ms} (raw edge - LIVE_EDGE_MARGIN_S, the "
            f"OLD/unchanged formula) -- self._live=False must never reach "
            f"the wall-clock branch (scoping-Orts6BRn-wallclock-playhead.md AC-W8)")

    def test_still_building_replay_never_calls_wall_clock_offset(self):
        """`_wall_clock_offset_ms` must never be invoked for a still-building
        replay -- guarded here by asserting the method, if/when it exists,
        raises if called. On the CURRENT code the method does not exist at
        all, so this assertion passes trivially both now and after PASS 2 --
        it exists to catch a future refactor that starts calling it for
        replay, not to prove PASS 2's absence today."""
        raw_edge_ms = 400_000
        engine = _make_engine(live=False, max_offset_ms=raw_edge_ms)

        def _boom():
            raise AssertionError(
                "_wall_clock_offset_ms() must not be called for a "
                "still-building replay (self._live=False)")

        engine._wall_clock_offset_ms = _boom  # no-op today; guards PASS 2's method
        # Should not raise.
        engine._live_edge_ms()


# ---------------------------------------------------------------------------
# AC-W4 (formerly AC7) -- quiet-period simulation. With data healthy
# throughout (heartbeat arriving every ~15.5s, well inside the 30s stale
# window) and NO audio edge, the wall-clock-driven ceiling must never fall
# behind the free-running clock -- ZERO frozen ticks, not "under some
# budget". Rewritten in place per
# scoping-Orts6BRn-wallclock-playhead.md §8 (AC-W4): "This assertion must be
# written as '0 frozen ticks,' not '<2.0s frozen' as the current AC7 file
# has it."
# ---------------------------------------------------------------------------

class QuietPeriodHeartbeatCadenceDoesNotFreezeTheClock(unittest.TestCase):
    """AC-W4. Drives the REAL `PlaybackClock.tick()` and the REAL
    `SessionEngine._live_edge_ms()` (both production code, unmodified) over
    two full ~15.5s heartbeat cycles, mirroring `_playback_loop()`'s own
    clamp snippet inline (as the PASS-1 AC7 test did). `time.monotonic` and
    `app.processing.session.datetime.now` are both patched off the same
    simulated "now" counter -- both `clock.py`/`session.py`'s health check
    read `time.monotonic`, and (post-fix) `_wall_clock_offset_ms` reads
    `datetime.now` -- so the fake wall-clock and the fake heartbeat cadence
    advance in lockstep, deterministically and without real sleeps."""

    def test_zero_frozen_ticks_across_two_heartbeat_cycles(self):
        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(live=True, max_offset_ms=0)
        engine._start_time = start

        from app.processing.clock import PlaybackClock
        engine._clock = PlaybackClock(start)

        heartbeat_interval_s = 15.5  # p95 measured cadence, scoping doc §2
        step_s = 0.1
        n_cycles = 2
        total_sim_s = heartbeat_interval_s * n_cycles

        fake_mono = [1_000_000.0]
        fake_elapsed_s = [0.0]

        def fake_monotonic():
            return fake_mono[0]

        with mock.patch("time.monotonic", side_effect=fake_monotonic), \
                _patch_wall_clock_now(start, lambda: fake_elapsed_s[0] * 1000):
            first_edge_ms = int(heartbeat_interval_s * 1000)
            engine._db.set_max_offset_ms(first_edge_ms)
            initial_ceiling_ms = engine._live_edge_ms()
            self.assertIsNotNone(initial_ceiling_ms)
            engine._clock.seek_to_offset(initial_ceiling_ms / 1000.0)
            engine._clock.play()

            frozen_time_s = 0.0
            elapsed_s = 0.0
            next_heartbeat_s = heartbeat_interval_s

            while elapsed_s < total_sim_s:
                fake_mono[0] += step_s
                fake_elapsed_s[0] += step_s
                elapsed_s += step_s

                if elapsed_s >= next_heartbeat_s:
                    # A heartbeat arrives: the data edge jumps to "now".
                    edge_ms = int((heartbeat_interval_s + elapsed_s) * 1000)
                    engine._db.set_max_offset_ms(edge_ms)
                    next_heartbeat_s += heartbeat_interval_s

                offset_before_ms = engine._clock.offset_seconds * 1000
                engine._clock.tick()

                # Mirrors session.py::_playback_loop's own clamp exactly.
                ceiling_ms = engine._live_edge_ms()
                if ceiling_ms is not None and engine._clock.offset_seconds * 1000 > ceiling_ms:
                    engine._clock.seek_to_offset(ceiling_ms / 1000.0)

                offset_after_ms = engine._clock.offset_seconds * 1000
                if offset_after_ms <= offset_before_ms + 1e-6:
                    # Clock wanted to advance (it's PLAYING) but didn't --
                    # frozen at the ceiling for this step.
                    frozen_time_s += step_s

        self.assertEqual(
            frozen_time_s, 0.0,
            f"Clock was frozen at the live-edge ceiling for {frozen_time_s:.1f}s "
            f"total across {n_cycles} heartbeat cycles ({total_sim_s:.1f}s "
            f"simulated) -- expected ZERO frozen ticks under the wall-clock "
            f"redesign (data stays healthy throughout; the ceiling should "
            f"never lag the free-running clock) "
            f"(scoping-Orts6BRn-wallclock-playhead.md AC-W4)")


# ---------------------------------------------------------------------------
# Coverage close-out (2026-08-21, Orts6BRn/cJJUzyAj patch-coverage gate) --
# `add_client()`'s own call site (session.py:554, `edge_ms =
# self._live_edge_ms()`) was, until this test, never called directly by any
# test in this suite -- every AC-W* test above exercises `_live_edge_ms()`
# itself and reasons (correctly, but only by inspection) that `add_client()`
# applies no further transformation to its return value. This test instead
# drives the real `add_client()` call site end-to-end so that reasoning is
# also a passing assertion, not just a comment.
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    """Stands in for `starlette.websockets.WebSocket` -- `add_client()` only
    ever calls `.send_text()` on it (via `_send_to_client`, which swallows
    any exception), so that's the only method this needs."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


class AddClientAppliesLiveEdgeCeilingOnFirstConnect(unittest.IsolatedAsyncioTestCase):
    """First client of a live engine's lifetime: `add_client()` seeks the
    clock to whatever `_live_edge_ms()` returns, unmodified (session.py:553-562).
    Uses the same wall-clock-driven, healthy-connection, no-audio scenario as
    `WallClockDrivenCeiling_NoAudio` (AC-W1/AC-W6) so the expected landing
    point is the raw wall-clock elapsed time -- proving the call site, not
    just the helper it calls."""

    async def test_first_client_lands_clock_at_live_edge_ms_return_value(self):
        from app.processing.clock import PlaybackClock

        start = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        elapsed_s = 51.4
        wall_ms = int(elapsed_s * 1000)
        data_ms = 999_000  # far above wall_ms; must NOT drive the result

        engine = _make_engine(live=True, max_offset_ms=data_ms)
        engine._start_time = start
        engine._clock = PlaybackClock(start)
        engine._duration = 3600.0
        engine._baseline_ready.set()  # skip the 30s connect-restore wait

        ws = _FakeWebSocket()

        with _patch_wall_clock_now(start, lambda: wall_ms):
            client_id = await engine.add_client(ws)

        self.assertEqual(client_id, 1)
        self.assertTrue(engine._initial_live_seek_done)
        self.assertAlmostEqual(
            engine._clock.offset_seconds, elapsed_s, places=3,
            msg=(
                f"add_client()'s first-connect seek landed the clock at "
                f"{engine._clock.offset_seconds}s, expected {elapsed_s}s "
                f"(raw wall-clock elapsed, matching _live_edge_ms()'s own "
                f"return value) -- session.py:554's call site diverged from "
                f"_live_edge_ms() itself"
            ))


if __name__ == "__main__":
    unittest.main()
