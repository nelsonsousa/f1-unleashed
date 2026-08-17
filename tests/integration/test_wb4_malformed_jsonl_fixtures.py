"""WB-4 (fix/wb4-preprocessor-run-failure-semantics) -- real-fixture coverage.

`tests/test_wb4_preprocessor_run_failure_semantics.py` proves WB-4's contract
("`SessionPreProcessor.run()` must not swallow an internal exception and
report a failed build as successful") using a *mocked* exception injected at
`SessionMessageBus.emit`. That is a legitimate seam test, but it never
exercises a real malformed `live.jsonl` going through the actual read/parse/
normalize path.

This file runs `SessionPreProcessor.run()` against each of the 12 real,
blind-generated fixtures in `tests/fixtures/malformed_live_jsonl/`
(`MANIFEST.md` there documents each corruption mode) -- UNMODIFIED file
bytes, real code, no mocking of exceptions. For every fixture the actual
pre-fix behaviour was observed first (see the investigation notes below and
in the PR/handoff summary) before any assertion was written, per
`.claude/agents/test-engineer.md` ("actually run it through the real code
path first").

## Structural note on the fixtures (2026-08-17-047 WB-1 resume update)

Every fixture's leading `SessionInfo` line (the "healthy session before
things went wrong" baseline the MANIFEST describes) carries a `Json` payload
with no `"Key"` field, e.g. `{"Meeting": "Silverstone", "Session": "Race"}`.
Historically (before this task) `SessionPreProcessor`'s gate matched on that
`Key` field, and none of these 12 fixtures' own leading `SessionInfo`
carries one -- so every fixture, run as-is, sat entirely in the pre-gate
buffer and built as a degenerate 0-message "complete" session regardless of
what corruption followed. Every test below used to prepend a synthetic,
Key-bearing `SessionInfo` line ahead of the fixture's own bytes purely to
work around that.

That workaround is gone. The gate is no longer `Key`-based at all
(DECISIONS.md #1's completion, `preprocessor.py`'s universal
60-minute-before-scheduled-start gate) -- with no `scheduled_start_utc`
passed to `SessionPreProcessor` (none of these tests need one; the fixtures'
own corruption is the thing under test, not gate timing), the gate is a
documented no-op (`_gate_cutoff is None` -> always keep, DECISIONS.md #3)
and EVERY message the loop sees, starting with the fixture's own first line,
survives and becomes `_start_time`. Each fixture's real, UNMODIFIED bytes
are run through `SessionPreProcessor.run()` directly now -- no prepended
line, no `Key` requirement, no workaround needed. `message_count`
expectations below are exactly what each fixture's own well-formed lines
produce (verified by direct run against the current code, not assumed --
this is one line lower than expectations pinned before this task, since
there is no longer a synthetic prepended line to also count).

## What was found

Three defensive layers sit between a malformed line and `run()`'s
try/except (the one WB-4 changed):

1. `file_reader.read_jsonl` -- catches `json.JSONDecodeError` per line and
   `continue`s (file_reader.py). A structurally-broken line (truncated,
   embedded raw control byte) never becomes a `RawLine` at all.
2. `stream_normalizer.StreamNormalizer._process_z` -- wraps
   `decompress_z_data` (base64 decode + zlib inflate + json.loads) in its own
   `except Exception: return []` (stream_normalizer.py). A `.z` topic with a
   corrupt payload is silently dropped, never reaching the bus.
3. `message_bus.SessionMessageBus.emit` -- wraps every individual handler
   call in `except Exception: logger.exception(...)` (message_bus.py). A
   processor bug on a given message cannot itself propagate to `run()`.

Only ONE of the 12 fixtures reaches past all three layers to actually
exercise WB-4's own catch/raise: `non_utf8_bytes.jsonl`. Its raw invalid
UTF-8 bytes break `TextIOWrapper` decoding *inside* `f.readline()` --
`UnicodeDecodeError`, not `json.JSONDecodeError` -- which layer 1's
per-line catch does not cover, and it happens outside `emit()` entirely, so
layer 3 never gets a chance either. It propagates through
`StreamNormalizer.normalize` and into `run()`'s own try/except, which is
exactly the code path WB-4 fixed. This is the one fixture in the set that
independently corroborates WB-4's fix against a real, not mocked, failure.

The other 11 corruption modes are each individually swallowed at layer 1 or
2 as designed -- not a WB-4 gap, and each is asserted below against its own
observed (not assumed) outcome, with the surviving well-formed lines in the
same file confirmed to still make it into the build (`message_count` > 0),
so a single bad line/entry does not take an otherwise-healthy capture down
with it.

Do NOT change `preprocessor.py`, `file_reader.py`, `stream_normalizer.py`,
or `message_bus.py` to make any of this differ -- this file records observed
behaviour of already-shipped WB-4 code plus its neighbouring layers, it does
not drive new production changes.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "malformed_live_jsonl"

_EXPECTED_KEY = 11330


def _make_session(root: Path, fixture_name: str) -> Path:
    """Build a session dir whose live.jsonl is the named fixture's real,
    unmodified bytes -- no synthetic prefix needed (see module docstring's
    "Structural note")."""
    sess = root / "2026" / "1290_Test_GP" / f"{_EXPECTED_KEY}_Race"
    sess.mkdir(parents=True)
    si = {"Key": _EXPECTED_KEY, "Type": "Race", "Name": "Race"}
    (sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

    fixture_bytes = (FIXTURE_DIR / fixture_name).read_bytes()
    with open(sess / "live.jsonl", "wb") as f:
        f.write(fixture_bytes)
    return sess


class Wb4MalformedJsonlFixtures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    async def _run(self, fixture_name: str):
        """Build a SessionPreProcessor against `fixture_name`'s real,
        unmodified bytes and run it to completion or failure. Returns
        (preprocessor, raised_exception)."""
        sess = _make_session(self.root, fixture_name)
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / f"{fixture_name}.db"):
            p = SessionPreProcessor(sess, "Race")
            raised = None
            try:
                await p.run()
            except Exception as e:  # noqa: BLE001 - capturing for inspection, not swallowing
                raised = e
            return p, raised

    # -- The one fixture that genuinely exercises WB-4's own catch/raise ----

    async def test_non_utf8_bytes_raises_and_marks_build_failed(self):
        """non_utf8_bytes.jsonl: raw invalid UTF-8 bytes spliced into a
        DriverList field break `TextIOWrapper` decoding inside
        `f.readline()` -- `UnicodeDecodeError`, which is NOT caught by
        `read_jsonl`'s per-line `json.JSONDecodeError` handler and happens
        outside `SessionMessageBus.emit`, so neither of the other two
        defensive layers gets a chance either. It propagates all the way
        into `run()`'s own try/except -- exactly the WB-4 contract: the
        build must not report success. Confirmed pre-fix-equivalent
        behaviour (this exercises code already fixed) by direct probing
        before writing this assertion.
        """
        p, raised = await self._run("non_utf8_bytes.jsonl")

        self.assertIsNotNone(
            raised,
            "non_utf8_bytes.jsonl must propagate an exception out of run() -- "
            "it is the one fixture that reaches WB-4's own catch/raise path"
        )
        self.assertIsInstance(raised, UnicodeDecodeError)
        self.assertTrue(
            p.failed,
            "run() must set self.failed=True before re-raising (WB-4 contract)"
        )
        self.assertEqual(
            p._db.get_meta("status"), "error",
            "run() must persist status=error before re-raising (WB-4 contract)"
        )
        p._db.close()

    # -- The eleven fixtures handled gracefully at a lower layer -------------

    async def test_truncated_final_line_is_dropped_not_fatal(self):
        """truncated_final_line.jsonl: final line cut mid base64, no closing
        quote/brace -- fails json.loads, caught by read_jsonl's per-line
        JSONDecodeError handler, silently skipped. The three well-formed
        lines ahead of it (SessionInfo, TrackStatus, one valid CarData.z)
        still make it through -- a crash mid-write loses only the partial
        last line, not the whole capture.
        """
        p, raised = await self._run("truncated_final_line.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "2")
        p._db.close()

    async def test_missing_type_field_is_processed_as_unrouted_topic_not_fatal(self):
        """missing_type_field.jsonl: structurally valid JSON with no "Type"
        key. `read_jsonl` defaults the topic to "" (`msg_data.get("Type",
        "")`) rather than raising -- the line becomes a real message with an
        empty topic, routed to no processor (logged as an unprocessed-topic
        discovery) but never crashes the build. All 4 well-formed lines
        (including this one) are counted.
        """
        p, raised = await self._run("missing_type_field.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "4")
        p._db.close()

    async def test_invalid_base64_z_payload_entry_is_dropped_not_fatal(self):
        """invalid_base64_z_payload.jsonl: a CarData.z line whose Json is not
        valid base64 at all. `StreamNormalizer._process_z` wraps
        `decompress_z_data` in its own try/except and drops the entry
        (`return []`) -- never reaches the bus, never reaches run()'s
        handler. The two well-formed CarData.z lines around it still decode
        and are forwarded.
        """
        p, raised = await self._run("invalid_base64_z_payload.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "1")
        p._db.close()

    async def test_invalid_zlib_z_payload_entry_is_dropped_not_fatal(self):
        """invalid_zlib_z_payload.jsonl: a Position.z line whose Json is
        valid base64 but the decoded bytes are not a valid zlib stream.
        Same `_process_z` try/except as the base64 case -- dropped, not
        fatal. The two well-formed Position.z lines around it survive.
        """
        p, raised = await self._run("invalid_zlib_z_payload.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "1")
        p._db.close()

    async def test_z_payload_decompresses_to_non_json_entry_is_dropped_not_fatal(self):
        """z_payload_decompresses_to_non_json.jsonl: a CarData.z line whose
        payload is valid base64 and valid zlib, but the decompressed bytes
        are plain text, not JSON. `decompress_z_data`'s trailing
        `json.loads(decompressed)` raises `json.JSONDecodeError`, caught by
        the same `_process_z` try/except as the other two `.z` cases --
        dropped, not fatal.
        """
        p, raised = await self._run("z_payload_decompresses_to_non_json.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "1")
        p._db.close()

    async def test_unparseable_datetime_entries_are_dropped_not_fatal(self):
        """unparseable_datetime.jsonl: three WeatherData lines with
        unparseable DateTime (empty string, free text, wrong format).
        `read_jsonl._parse_timestamp` returns None for each, and
        `if not envelope_ts: continue` drops the line before it is ever
        yielded -- never reaches the normalizer or the bus. The two
        well-formed lines (TrackStatus, and the one WeatherData with a
        valid timestamp) survive.
        """
        p, raised = await self._run("unparseable_datetime.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "3")
        p._db.close()

    async def test_embedded_null_byte_line_is_dropped_not_fatal(self):
        """embedded_null_byte.jsonl: a RaceControlMessages line with a
        literal (unescaped) \\x00 byte inside a JSON string value. Confirmed
        directly: `json.loads` on that exact line raises
        `json.JSONDecodeError` ("Invalid control character") under Python's
        default strict mode -- caught by read_jsonl's per-line handler like
        any other malformed line, never reaches a processor. The other
        well-formed RaceControlMessages line survives.
        """
        p, raised = await self._run("embedded_null_byte.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "3")
        p._db.close()

    async def test_empty_file_builds_degenerate_but_valid(self):
        """empty_file.jsonl: 0 bytes. `read_jsonl` hits EOF on its very
        first `readline()` and (not tail-following) breaks immediately --
        no exception. No message ever reaches the loop at all, so
        `message_count` is 0 and `_start_time` stays unset; this matches the
        intended "degenerate session builds, doesn't crash" behaviour
        already pinned by tests/test_preprocess_degenerate.py (B02) for the
        SessionInfo-only case, extended here to the zero-content case.
        """
        p, raised = await self._run("empty_file.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "0")
        p._db.close()

    async def test_single_truncated_line_builds_degenerate_but_valid(self):
        """single_truncated_line.jsonl: exactly one line, truncated mid
        token, no valid content at all. Fails json.loads, dropped by
        read_jsonl's per-line handler exactly like any other malformed
        line -- no message ever reaches the loop, so the build completes
        degenerate (message_count 0) rather than crashing.
        """
        p, raised = await self._run("single_truncated_line.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "0")
        p._db.close()

    async def test_corrupted_line_in_middle_is_dropped_not_fatal(self):
        """corrupted_line_in_middle.jsonl: 7 well-formed lines surround one
        CarData.z line truncated mid base64 (no trailing "}}") -- a single
        transient write glitch in an otherwise healthy capture, per the
        MANIFEST. The corrupted line fails json.loads and is dropped by
        read_jsonl's per-line handler; all 7 well-formed lines around it are
        still processed -- the single glitch does not take the whole
        capture down, which is the behaviour this fixture exists to prove.
        """
        p, raised = await self._run("corrupted_line_in_middle.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "4")
        p._db.close()

    async def test_mixed_line_endings_all_lines_processed_not_fatal(self):
        """mixed_line_endings.jsonl: some lines end \\r\\n, some bare \\n,
        within the same file; each line's JSON is individually valid.
        Python's text-mode file handle (universal newlines) normalizes both
        transparently and `line.strip()` removes any residual \\r -- every
        line parses. Included as a genuine "looks malformed but isn't"
        control case: confirms the file-format quirk alone never trips
        run()'s exception handler.
        """
        p, raised = await self._run("mixed_line_endings.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(p._db.get_meta("message_count"), "5")
        p._db.close()

    # -- Documents that the old fixture-generation gap (module docstring's
    # "Structural note") is now resolved by the universal gate itself -------

    async def test_fixture_as_provided_now_processes_normally_no_workaround_needed(self):
        """This test used to document a real gap: none of these 12
        fixtures' own leading `SessionInfo` carries a `Key`, so the old
        `Key`-matching gate could never open for any of them as-provided,
        and every test in this file had to prepend a synthetic Key-bearing
        line to work around it (see the module docstring's old "Structural
        note", now updated). With the `Key`-based gate gone (DECISIONS.md
        #1's completion) and no `scheduled_start_utc` passed here (a no-op
        gate, DECISIONS.md #3), `corrupted_line_in_middle.jsonl` run EXACTLY
        as delivered -- no prefix, no workaround -- now processes normally:
        the fixture's own first (Key-less) SessionInfo line becomes
        `_start_time` immediately, and every other well-formed line in the
        file is processed and counted, exactly matching
        `test_corrupted_line_in_middle_is_dropped_not_fatal`'s own
        already-correct expectation (this file's `_run` no longer takes a
        `prefix_gate_opener` argument at all -- there is nothing left to
        toggle).
        """
        p, raised = await self._run("corrupted_line_in_middle.jsonl")
        self.assertIsNone(raised)
        self.assertFalse(p.failed)
        self.assertEqual(p._db.get_meta("status"), "complete")
        self.assertEqual(
            p._db.get_meta("message_count"), "4",
            "as-provided (no synthetic prefix, no Key on the fixture's own "
            "SessionInfo line), every well-formed line is now processed -- "
            "the universal gate has no Key requirement at all, closing the "
            "fixture-generation gap the old Key-matching gate created"
        )
        p._db.close()


if __name__ == "__main__":
    unittest.main()
