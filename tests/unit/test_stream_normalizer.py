"""StreamNormalizer — written from the closed design's acceptance criteria
(`docs/artifacts/2026-07-27-003-pipeline-redesign/requirement-spec.md` §9.8,
`architecture-plan.md` §A.9.4), not from the implementation.

Covers:
  - AC-4: causal, single-pass, no-lookahead utcTimestamp/offsetMs.
  - AC-5: CarData.z/Position.z/ExtrapolatedClock bypass STREAM_LAG; every
    other topic (Heartbeat included) uses the generic envelope-STREAM_LAG
    rule with no topic-specific exception.
  - The universal 60-minute-before-scheduled-start gate (§9.2), including
    zombie-message-shaped cases (very stale payloads).
  - The continuous dedup rule (§9.1) for singleton and array/indexed topics,
    and for .z entries.
  - AC-10': the accepted (undefended) risk that a genuinely-changed-but-stale
    EC update still updates STREAM_LAG — no clamp.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.processing.file_reader import RawLine
from app.processing.stream_normalizer import StreamNormalizer, SYNTHETIC_TOPICS


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _line(topic: str, data, envelope_ts: str) -> RawLine:
    return RawLine(topic=topic, data=data, envelope_ts=_dt(envelope_ts))


class GenericTimestampRule(unittest.TestCase):
    """AC-5: everything except CarData.z/Position.z/ExtrapolatedClock uses
    envelope - STREAM_LAG, with NO exception for Heartbeat."""

    def test_heartbeat_uses_generic_rule_no_exception(self):
        norm = StreamNormalizer()
        # EC sets STREAM_LAG = envelope(10:00:02) - payload(10:00:00) = 2s
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"},
                                 "2026-07-18T10:00:02.000Z"))
        out = norm.process_line(_line("Heartbeat", {"n": 1}, "2026-07-18T10:00:05.000Z"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:03.000Z"))

    def test_generic_topic_uses_generic_rule(self):
        norm = StreamNormalizer()
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"},
                                 "2026-07-18T10:00:02.000Z"))
        out = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T10:00:10.000Z"))
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:08.000Z"))

    def test_extrapolated_clock_bypasses_stream_lag_for_its_own_timestamp(self):
        norm = StreamNormalizer()
        out = norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"},
                                       "2026-07-18T10:00:02.000Z"))
        # EC's own utcTimestamp falls out to the payload timestamp, not the
        # (yet-to-be-updated) envelope value — envelope - (envelope-payload) = payload.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:00.000Z"))

    def test_car_data_z_bypasses_stream_lag_uses_own_payload_ts(self):
        norm = StreamNormalizer()
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"},
                                 "2026-07-18T10:00:02.000Z"))
        z_line = _line("CarData.z", _compress_car_data([
            {"Utc": "2026-07-18T10:00:20.000Z", "Cars": {"1": {"Channels": {"2": 300}}}},
        ]), "2026-07-18T10:00:22.500Z")
        out = norm.process_line(z_line)
        # AC-1: a lone CarData.z/Position.z entry is now held in the reorder
        # buffer, not emitted synchronously — nothing has yet advanced the
        # watermark past it (there is nothing else to reorder against).
        # flush() (AC-3's end-of-stream release) surfaces it for this
        # single-entry timestamp-computation check, which is what this test
        # actually exercises.
        self.assertEqual(out, [])
        out = norm.flush()
        self.assertEqual(len(out), 1)
        # Uses the entry's own payload Utc, NOT envelope(10:00:22.5) - lag(2s).
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:20.000Z"))

    def test_position_z_bypasses_stream_lag_uses_own_payload_ts(self):
        norm = StreamNormalizer()
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"},
                                 "2026-07-18T10:00:02.000Z"))
        z_line = _line("Position.z", _compress_position([
            {"Timestamp": "2026-07-18T10:00:30.000Z", "Entries": {"1": [0, 0, 5.0]}},
        ]), "2026-07-18T10:00:32.700Z")
        out = norm.process_line(z_line)
        # AC-1: see the CarData.z case above — a lone .z entry is held, not
        # emitted synchronously.
        self.assertEqual(out, [])
        out = norm.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:30.000Z"))


class CausalSinglePass(unittest.TestCase):
    """AC-4: no lookahead — feeding lines one at a time (vs all at once)
    produces identical output; STREAM_LAG only reflects EC lines seen so far."""

    def test_stream_lag_only_reflects_lines_seen_so_far(self):
        norm = StreamNormalizer()
        before = norm.process_line(_line("Heartbeat", {"n": 1}, "2026-07-18T10:00:00.000Z"))
        self.assertEqual(before[0].utc_timestamp, _dt("2026-07-18T10:00:00.000Z"))  # lag=0, no EC yet
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:01:00.000Z"},
                                 "2026-07-18T10:01:03.000Z"))
        # Different payload from the first Heartbeat — content-dedup (§9.1) would
        # otherwise suppress a byte-identical repeat regardless of timestamp.
        after = norm.process_line(_line("Heartbeat", {"n": 2}, "2026-07-18T10:02:00.000Z"))
        self.assertEqual(after[0].utc_timestamp, _dt("2026-07-18T10:01:57.000Z"))  # lag now 3s

    def test_replaying_same_lines_twice_is_byte_identical(self):
        lines = [
            _line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"}, "2026-07-18T10:00:01.000Z"),
            _line("Heartbeat", {"n": 1}, "2026-07-18T10:00:05.000Z"),
            _line("Heartbeat", {"n": 2}, "2026-07-18T10:00:10.000Z"),
        ]

        def run():
            n = StreamNormalizer()
            out = []
            for ln in lines:
                out.extend(n.process_line(ln))
            return [(m.topic, m.utc_timestamp, m.offset_ms) for m in out]

        self.assertEqual(run(), run())


class UniversalGate(unittest.TestCase):
    """§9.2: discard any message whose payload timestamp is >60 min before
    the session's SCHEDULED start — replaces the old SessionInfo.Key gate and
    the old pre-start .z skip with one rule."""

    def test_message_far_before_scheduled_start_is_dropped(self):
        scheduled = _dt("2026-07-18T14:00:00.000Z")
        norm = StreamNormalizer(scheduled_start_utc=scheduled)
        # 283.6 hours stale (a real confirmed zombie-message magnitude).
        stale_envelope = scheduled - timedelta(hours=283.6)
        out = norm.process_line(_line("Heartbeat", {}, stale_envelope.isoformat().replace("+00:00", "Z")))
        self.assertEqual(out, [])
        self.assertEqual(norm.counters["Heartbeat"]["gate_dropped"], 1)

    def test_message_2h_stale_is_dropped(self):
        scheduled = _dt("2026-07-18T14:00:00.000Z")
        norm = StreamNormalizer(scheduled_start_utc=scheduled)
        stale_envelope = scheduled - timedelta(hours=2.16)
        out = norm.process_line(_line("Heartbeat", {}, stale_envelope.isoformat().replace("+00:00", "Z")))
        self.assertEqual(out, [])

    def test_message_within_60_minutes_before_start_survives(self):
        scheduled = _dt("2026-07-18T14:00:00.000Z")
        norm = StreamNormalizer(scheduled_start_utc=scheduled)
        pre_session = scheduled - timedelta(minutes=40)
        out = norm.process_line(_line("Heartbeat", {}, pre_session.isoformat().replace("+00:00", "Z")))
        self.assertEqual(len(out), 1)

    def test_z_topic_entries_are_gated_too(self):
        """The old pipeline had a SEPARATE pre-start .z skip; the universal
        gate must cover .z entries as well, with no special case (§9.2)."""
        scheduled = _dt("2026-07-18T14:00:00.000Z")
        norm = StreamNormalizer(scheduled_start_utc=scheduled)
        stale_ts = (scheduled - timedelta(hours=21)).isoformat().replace("+00:00", "Z")
        z_line = _line("CarData.z", _compress_car_data([
            {"Utc": stale_ts, "Cars": {"1": {"Channels": {"2": 300}}}},
        ]), scheduled.isoformat().replace("+00:00", "Z"))
        out = norm.process_line(z_line)
        self.assertEqual(out, [])

    def test_no_scheduled_start_means_gate_is_a_no_op(self):
        norm = StreamNormalizer(scheduled_start_utc=None)
        very_old = "2020-01-01T00:00:00.000Z"
        out = norm.process_line(_line("Heartbeat", {}, very_old))
        self.assertEqual(len(out), 1)

    def test_reference_is_first_message_to_survive_the_gate(self):
        scheduled = _dt("2026-07-18T14:00:00.000Z")
        norm = StreamNormalizer(scheduled_start_utc=scheduled)
        # Dropped by the gate — must NOT become the reference.
        stale_envelope = scheduled - timedelta(hours=5)
        norm.process_line(_line("Heartbeat", {}, stale_envelope.isoformat().replace("+00:00", "Z")))
        self.assertIsNone(norm.reference_utc)
        survivor_envelope = scheduled - timedelta(minutes=10)
        out = norm.process_line(_line("Heartbeat", {"n": 1}, survivor_envelope.isoformat().replace("+00:00", "Z")))
        self.assertEqual(norm.reference_utc, out[0].utc_timestamp)
        self.assertEqual(out[0].offset_ms, 0)

    def test_gate_runs_before_stream_lag_commit_zombie_ec_does_not_poison_it(self):
        """Regression (verification/fix-attempt-2): the gate must run "once
        per message... before anything else in the normalizer"
        (requirement-spec.md §9.2). Pre-fix, STREAM_LAG was updated from a
        zombie ExtrapolatedClock message BEFORE the gate ran, so even though
        the gate correctly dropped that single zombie message, STREAM_LAG
        stayed poisoned for every SUBSEQUENT message — silently corrupting
        their utcTimestamp and, if stale enough, gating them out too."""
        scheduled = _dt("2026-07-18T14:00:00.000Z")
        norm = StreamNormalizer(scheduled_start_utc=scheduled)

        # Zombie EC: arrives in-window (envelope 13:50, within the 60-min
        # gate) but its OWN payload Utc is a real confirmed zombie-message
        # magnitude of staleness — hours before the gate cutoff (13:00).
        zombie_envelope = "2026-07-18T13:50:00.000Z"
        zombie_payload_utc = "2026-07-18T05:00:00.000Z"
        out = norm.process_line(_line("ExtrapolatedClock", {"Utc": zombie_payload_utc}, zombie_envelope))
        self.assertEqual(out, [])                      # dropped by the gate
        self.assertEqual(norm.stream_lag_s, 0.0)        # NOT committed/poisoned

        # A healthy Heartbeat shortly after must compute a sane utc_timestamp
        # — if STREAM_LAG had been poisoned to ~8h50m by the zombie, this
        # message would itself compute a wildly wrong utcTimestamp and be
        # silently dropped by the gate too.
        healthy = norm.process_line(_line("Heartbeat", {"n": 1}, "2026-07-18T13:55:00.000Z"))
        self.assertEqual(len(healthy), 1)
        self.assertEqual(healthy[0].utc_timestamp, _dt("2026-07-18T13:55:00.000Z"))

        # A subsequent HEALTHY ExtrapolatedClock update must still correctly
        # update STREAM_LAG once it survives the gate — the fix only defers
        # the commit, it doesn't disable it.
        healthy_ec = norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T13:58:00.000Z"},
                                              "2026-07-18T13:58:02.000Z"))
        self.assertEqual(len(healthy_ec), 1)
        self.assertAlmostEqual(norm.stream_lag_s, 2.0)

        # And a message after that healthy EC reflects the correct 2s lag.
        after = norm.process_line(_line("Heartbeat", {"n": 2}, "2026-07-18T13:59:00.000Z"))
        self.assertEqual(after[0].utc_timestamp, _dt("2026-07-18T13:58:58.000Z"))


class DedupEnabledToggle(unittest.TestCase):
    """`set_dedup_enabled()` (verification/fix-attempt-2 regression): lets a
    caller with its own pre-forwarding buffering stage — preprocessor.py's
    SessionInfo.Key gate-buffer, DECISIONS.md #1 — prevent a message that
    will later be discarded (never actually forwarded downstream) from
    consuming/poisoning the normalizer's dedup state and silently
    suppressing the REAL subsequent message for that topic."""

    def test_message_processed_with_dedup_disabled_is_never_suppressed(self):
        norm = StreamNormalizer()
        norm.set_dedup_enabled(False)
        first = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T09:59:50.000Z"))
        self.assertEqual(len(first), 1)
        # A byte-identical repeat, still with dedup disabled — must still
        # NOT be suppressed (this simulates a buffered-then-discarded
        # duplicate, e.g. a duplicate-content TrackStatus that isn't the
        # SessionInfo-matching one).
        repeat = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T09:59:55.000Z"))
        self.assertEqual(len(repeat), 1)

    def test_message_processed_with_dedup_disabled_does_not_poison_state_for_later(self):
        norm = StreamNormalizer()
        norm.set_dedup_enabled(False)
        norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T09:59:50.000Z"))

        norm.set_dedup_enabled(True)
        # The REAL TrackStatus, same content as the earlier (never actually
        # forwarded downstream) one — must still be forwarded now that dedup
        # is enabled, because the earlier message never committed dedup
        # state while dedup was disabled.
        real = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T10:00:02.000Z"))
        self.assertEqual(len(real), 1)

    def test_dedup_enabled_by_default(self):
        """Standalone use (no separate buffering caller) still gets dedup as
        documented — the toggle is opt-out, not opt-in."""
        norm = StreamNormalizer()
        norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T09:59:50.000Z"))
        repeat = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T10:00:02.000Z"))
        self.assertEqual(repeat, [])


class ContinuousDedup(unittest.TestCase):
    """§9.1: one continuous, always-on dedup rule, no burst/marker concept."""

    def test_byte_identical_singleton_repeat_is_suppressed(self):
        norm = StreamNormalizer()
        first = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T10:00:00.000Z"))
        self.assertEqual(len(first), 1)
        repeat = norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T10:05:00.000Z"))
        self.assertEqual(repeat, [])
        self.assertEqual(norm.counters["TrackStatus"]["dedup_suppressed"], 1)

    def test_changed_singleton_payload_is_forwarded(self):
        norm = StreamNormalizer()
        norm.process_line(_line("TrackStatus", {"Status": "1"}, "2026-07-18T10:00:00.000Z"))
        changed = norm.process_line(_line("TrackStatus", {"Status": "2"}, "2026-07-18T10:05:00.000Z"))
        self.assertEqual(len(changed), 1)

    def test_rcm_resent_array_only_forwards_genuinely_new_entries(self):
        """Directly modelled on the Budapest Qualifying proof cited in
        requirement-spec.md §9.1: a reconnect resends the whole RCM array
        verbatim; only entries past the watermark should re-emerge."""
        norm = StreamNormalizer()
        first = norm.process_line(_line(
            "RaceControlMessages",
            {"Messages": {"0": {"Message": "GREEN"}, "1": {"Message": "YELLOW"}}},
            "2026-07-18T10:00:00.000Z",
        ))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].data, {"Messages": {"0": {"Message": "GREEN"}, "1": {"Message": "YELLOW"}}})

        # Reconnect: F1 resends the full history (0,1) plus one genuinely new entry (2).
        resent = norm.process_line(_line(
            "RaceControlMessages",
            {"Messages": {
                "0": {"Message": "GREEN"},
                "1": {"Message": "YELLOW"},
                "2": {"Message": "SAFETY CAR"},
            }},
            "2026-07-18T10:10:00.000Z",
        ))
        self.assertEqual(len(resent), 1)
        self.assertEqual(resent[0].data, {"Messages": {"2": {"Message": "SAFETY CAR"}}})

    def test_rcm_exact_resend_with_nothing_new_is_fully_suppressed(self):
        norm = StreamNormalizer()
        norm.process_line(_line(
            "RaceControlMessages", {"Messages": {"0": {"Message": "GREEN"}}},
            "2026-07-18T10:00:00.000Z",
        ))
        resent = norm.process_line(_line(
            "RaceControlMessages", {"Messages": {"0": {"Message": "GREEN"}}},
            "2026-07-18T10:10:00.000Z",
        ))
        self.assertEqual(resent, [])
        self.assertEqual(norm.counters["RaceControlMessages"]["dedup_suppressed"], 1)

    def test_session_data_status_series_high_water_mark(self):
        norm = StreamNormalizer()
        norm.process_line(_line(
            "SessionData", {"StatusSeries": {"0": {"SessionStatus": "Inactive"}}},
            "2026-07-18T10:00:00.000Z",
        ))
        resent = norm.process_line(_line(
            "SessionData",
            {"StatusSeries": {"0": {"SessionStatus": "Inactive"}, "3": {"SessionStatus": "Started"}}},
            "2026-07-18T10:10:00.000Z",
        ))
        self.assertEqual(resent[0].data, {"StatusSeries": {"3": {"SessionStatus": "Started"}}})

    def test_z_topic_resent_burst_only_forwards_new_entries(self):
        # AC-1: entries are now held in the reorder buffer rather than
        # emitted synchronously, so exactly WHEN a given entry is released
        # (this call vs. a later one vs. flush()) shifts once buffering is
        # in the mix — see the CarData.z/Position.z timestamp tests above.
        # What must still hold (the actual thing this test exercises) is
        # dedup correctness: the resent burst's two ALREADY-SEEN entries
        # must never be forwarded a second time, so across every call plus
        # a final flush(), each distinct payload timestamp is released
        # EXACTLY once.
        norm = StreamNormalizer()
        first = norm.process_line(_line("CarData.z", _compress_car_data([
            {"Utc": "2026-07-18T10:00:00.000Z", "Cars": {"1": {"Channels": {"2": 100}}}},
            {"Utc": "2026-07-18T10:00:01.000Z", "Cars": {"1": {"Channels": {"2": 110}}}},
        ]), "2026-07-18T10:00:02.000Z"))

        # Reconnect resend: same two entries again, plus one genuinely new one.
        resent = norm.process_line(_line("CarData.z", _compress_car_data([
            {"Utc": "2026-07-18T10:00:00.000Z", "Cars": {"1": {"Channels": {"2": 100}}}},
            {"Utc": "2026-07-18T10:00:01.000Z", "Cars": {"1": {"Channels": {"2": 110}}}},
            {"Utc": "2026-07-18T10:00:02.000Z", "Cars": {"1": {"Channels": {"2": 120}}}},
        ]), "2026-07-18T10:00:05.000Z"))

        released = first + resent + norm.flush()
        timestamps = [m.utc_timestamp for m in released]
        self.assertEqual(
            timestamps,
            [_dt("2026-07-18T10:00:00.000Z"), _dt("2026-07-18T10:00:01.000Z"),
             _dt("2026-07-18T10:00:02.000Z")],
            "the resent burst's two already-seen entries must not be forwarded "
            "twice, and the genuinely new entry (t=2) must still get through",
        )


class DefensiveFallbackBranches(unittest.TestCase):
    """Coverage for defensive fallback branches not otherwise exercised by
    the acceptance-criteria-driven test classes above."""

    def test_split_z_entries_unknown_z_topic_falls_back_to_whole_payload(self):
        from app.processing.stream_normalizer import split_z_entries
        out = split_z_entries("Other.z", {"anything": True})
        self.assertEqual(out, [(None, {"anything": True})])

    def test_z_entry_with_no_own_timestamp_falls_back_to_envelope_ts(self):
        """CarData.z entries missing their own `Utc` never reach
        `split_z_entries`'s per-entry branch (it only appends entries that
        HAVE a Utc) — a payload with none produces its own [(None, data)]
        fallback, exercising `_process_z`'s own envelope-ts fallback."""
        norm = StreamNormalizer()
        z_line = _line("CarData.z", _compress_car_data([
            {"Cars": {"1": {"Channels": {"2": 300}}}},  # no "Utc" key
        ]), "2026-07-18T10:00:22.500Z")
        out = norm.process_line(z_line)
        # AC-1: held in the reorder buffer, not emitted synchronously — see
        # the CarData.z/Position.z timestamp tests above.
        self.assertEqual(out, [])
        out = norm.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:22.500Z"))

    def test_session_data_with_neither_series_nor_status_series_has_no_index_keys(self):
        from app.processing.stream_normalizer import _entry_index_keys
        self.assertIsNone(_entry_index_keys("SessionData", {"SomethingElse": {}}))

    def test_entry_index_keys_unrecognized_topic_returns_none(self):
        from app.processing.stream_normalizer import _entry_index_keys
        self.assertIsNone(_entry_index_keys("SomeOtherTopic", {"Messages": {"0": {}}}))

    def test_position_z_entry_with_no_own_timestamp_falls_back_to_whole_payload(self):
        norm = StreamNormalizer()
        z_line = _line("Position.z", _compress_position([
            {"Entries": {"1": [0, 0, 5.0]}},  # no "Timestamp" key
        ]), "2026-07-18T10:00:22.500Z")
        out = norm.process_line(z_line)
        # AC-1: held in the reorder buffer, not emitted synchronously — see
        # the CarData.z/Position.z timestamp tests above.
        self.assertEqual(out, [])
        out = norm.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].utc_timestamp, _dt("2026-07-18T10:00:22.500Z"))

    def test_cumulative_dedup_second_message_advances_watermark_across_two_calls(self):
        """Exercises the high-water-mark advancing across SEPARATE
        `_dedup_cumulative` calls (not just within one sorted entry loop) —
        the watermark carried from a first message must correctly gate a
        second message's entries too."""
        norm = StreamNormalizer()
        first = norm.process_line(_line(
            "RaceControlMessages", {"Messages": {"0": {"Message": "GREEN"}}},
            "2026-07-18T10:00:00.000Z",
        ))
        self.assertEqual(len(first), 1)
        second = norm.process_line(_line(
            "RaceControlMessages",
            {"Messages": {"0": {"Message": "GREEN"}, "1": {"Message": "YELLOW"},
                           "2": {"Message": "SAFETY CAR"}}},
            "2026-07-18T10:05:00.000Z",
        ))
        self.assertEqual(second[0].data, {"Messages": {
            "1": {"Message": "YELLOW"}, "2": {"Message": "SAFETY CAR"},
        }})

    def test_rebuild_cumulative_payload_unknown_topic_returns_original(self):
        from app.processing.stream_normalizer import _rebuild_cumulative_payload
        original = {"whatever": "shape"}
        self.assertIs(_rebuild_cumulative_payload("SomeOtherTopic", original, {"0": {}}), original)

    def test_sort_key_falls_back_to_lexicographic_for_non_numeric_key(self):
        from app.processing.stream_normalizer import _sort_key
        self.assertEqual(_sort_key("not-a-number"), (1, "not-a-number"))
        self.assertEqual(_sort_key("3"), (0, 3))


class SyntheticTopics(unittest.TestCase):
    def test_session_end_marker_never_forwarded(self):
        norm = StreamNormalizer()
        self.assertIn("_SessionEnd", SYNTHETIC_TOPICS)
        out = norm.process_line(_line("_SessionEnd", {}, "2026-07-18T10:00:00.000Z"))
        self.assertEqual(out, [])


class MalformedInputIsHandledDefensively(unittest.TestCase):
    def test_ec_with_non_dict_payload_leaves_stream_lag_unchanged(self):
        norm = StreamNormalizer()
        norm.process_line(_line("ExtrapolatedClock", "not-a-dict", "2026-07-18T10:00:00.000Z"))
        self.assertEqual(norm.stream_lag_s, 0.0)

    def test_ec_with_missing_utc_leaves_stream_lag_unchanged(self):
        norm = StreamNormalizer()
        norm.process_line(_line("ExtrapolatedClock", {"Remaining": "01:00:00"}, "2026-07-18T10:00:00.000Z"))
        self.assertEqual(norm.stream_lag_s, 0.0)

    def test_z_topic_non_string_payload_is_ignored(self):
        norm = StreamNormalizer()
        out = norm.process_line(_line("CarData.z", {"already": "decoded"}, "2026-07-18T10:00:00.000Z"))
        self.assertEqual(out, [])

    def test_z_topic_corrupt_base64_is_ignored_not_raised(self):
        norm = StreamNormalizer()
        out = norm.process_line(_line("CarData.z", "not-valid-base64-zlib!!", "2026-07-18T10:00:00.000Z"))
        self.assertEqual(out, [])

    def test_cumulative_topic_non_dict_payload_falls_back_to_singleton_dedup(self):
        norm = StreamNormalizer()
        first = norm.process_line(_line("RaceControlMessages", "not-a-dict", "2026-07-18T10:00:00.000Z"))
        self.assertEqual(len(first), 1)
        repeat = norm.process_line(_line("RaceControlMessages", "not-a-dict", "2026-07-18T10:05:00.000Z"))
        self.assertEqual(repeat, [])   # byte-identical singleton repeat suppressed


class CumulativeListEdgeCases(unittest.TestCase):
    def test_rcm_messages_as_a_list_is_normalized_to_keyed_dict(self):
        norm = StreamNormalizer()
        first = norm.process_line(_line(
            "RaceControlMessages", {"Messages": [{"Message": "GREEN"}, {"Message": "YELLOW"}]},
            "2026-07-18T10:00:00.000Z",
        ))
        self.assertEqual(len(first), 1)
        resent = norm.process_line(_line(
            "RaceControlMessages",
            {"Messages": [{"Message": "GREEN"}, {"Message": "YELLOW"}, {"Message": "SC"}]},
            "2026-07-18T10:10:00.000Z",
        ))
        self.assertEqual(resent[0].data, {"Messages": {"2": {"Message": "SC"}}})

    def test_session_data_falls_back_to_series_when_no_status_series(self):
        norm = StreamNormalizer()
        norm.process_line(_line("SessionData", {"Series": {"0": {"a": 1}}}, "2026-07-18T10:00:00.000Z"))
        resent = norm.process_line(_line(
            "SessionData", {"Series": {"0": {"a": 1}, "1": {"a": 2}}}, "2026-07-18T10:10:00.000Z"))
        self.assertEqual(resent[0].data, {"Series": {"1": {"a": 2}}})

    def test_watermark_entry_changed_on_resend_logs_but_does_not_raise(self):
        """§9.1's invariant check: the watermark entry re-appearing with
        DIFFERENT content is unexpected but must not crash — just warn and
        proceed with the key-based filter regardless."""
        norm = StreamNormalizer()
        norm.process_line(_line(
            "RaceControlMessages", {"Messages": {"0": {"Message": "GREEN"}}},
            "2026-07-18T10:00:00.000Z",
        ))
        with self.assertLogs("app.processing.stream_normalizer", level="WARNING") as log:
            resent = norm.process_line(_line(
                "RaceControlMessages",
                {"Messages": {"0": {"Message": "CHANGED"}, "1": {"Message": "NEW"}}},
                "2026-07-18T10:10:00.000Z",
            ))
        self.assertTrue(any("watermark entry" in m for m in log.output))
        self.assertEqual(resent[0].data, {"Messages": {"1": {"Message": "NEW"}}})


class AcceptedRiskNoClamp(unittest.TestCase):
    """AC-10' (§9.1/§9.8): the residual poisoning risk from a genuinely-
    changed-but-stale EC update is explicitly ACCEPTED, not defended
    against. This test documents that acceptance rather than silently
    passing over an undefended gap."""

    def test_a_changed_but_stale_ec_update_does_poison_stream_lag(self):
        norm = StreamNormalizer()
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T10:00:00.000Z"},
                                 "2026-07-18T10:00:01.000Z"))
        self.assertEqual(norm.stream_lag_s, 1.0)
        # A wildly stale-but-different EC value (queued during a long outage)
        # still updates STREAM_LAG — no clamp exists to reject it.
        norm.process_line(_line("ExtrapolatedClock", {"Utc": "2026-07-18T08:00:00.000Z"},
                                 "2026-07-18T10:05:00.000Z"))
        self.assertAlmostEqual(norm.stream_lag_s, 7500.0)  # 2h5m — accepted, not clamped


# -- .z fixture helpers (compress the way the real client does) -----------------

import base64
import json
import zlib


def _compress(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = co.compress(raw) + co.flush()
    return base64.b64encode(compressed).decode("ascii")


def _compress_car_data(entries: list[dict]) -> str:
    return _compress({"Entries": entries})


def _compress_position(entries: list[dict]) -> str:
    return _compress({"Position": entries})


if __name__ == "__main__":
    unittest.main()
