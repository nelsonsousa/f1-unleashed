---
task: WB-4 — real-fixture (non-mocked) validation against 12 blind-generated malformed live.jsonl captures
kind: supplementary integration coverage (not a red-gate — WB-4 is already implemented and green-gate verified)
basis:
  - docs/artifacts/2026-07-28-011-wb4-run-failure-semantics/test-plan.md (original mocked red-gate)
  - tests/fixtures/malformed_live_jsonl/MANIFEST.md
status: GREEN — all 13 tests pass against the current (fixed) code, confirmed to genuinely
  exercise the code (see "Sanity check" below)
---

# Test plan — WB-4 real-fixture coverage

## Why this exists

`tests/test_wb4_preprocessor_run_failure_semantics.py` proves WB-4's contract with a
*mocked* exception (`SessionMessageBus.emit` patched to raise `RuntimeError` for one
topic). That is a legitimate seam test but never exercises a real malformed
`live.jsonl` through the actual parse/decompress/normalize path. A separate, blind
agent (spec-only, no codebase access) generated 12 realistic corrupted `live.jsonl`
fixtures at `tests/fixtures/malformed_live_jsonl/` (see `MANIFEST.md` there). This
plan covers running `SessionPreProcessor.run()` against each, unmodified, for real.

**Test file**: `tests/integration/test_wb4_malformed_jsonl_fixtures.py`

## Investigation before writing assertions

Per test-engineer discipline, each fixture was run through the real code first (ad hoc
probe script, not committed) and the actual outcome observed before any assertion was
written. Two probes were run: fixtures as delivered, and fixtures with one extra
Key-bearing `SessionInfo` line prepended.

**Structural finding (not a WB-4 defect)**: every fixture's own leading `SessionInfo`
line lacks a `"Key"` field (the blind generator had no way to know this codebase's
gate-open check requires `msg.data.get("Key") == self._expected_key`). Run any of the
12 fixtures exactly as delivered and the `SessionInfo.Key` gate never opens — every
build completes as an empty "complete" build regardless of the corruption further
down the file. This is true of all 12 uniformly and is documented, with one direct
demonstration, in `test_fixture_as_provided_never_opens_the_sessioninfo_gate`. To
actually exercise each corruption's handling, every other test prepends one
Key-bearing `SessionInfo` line (own file, own bytes) so the fixture's own bytes reach
normal processing — this changes nothing about the corruption itself.

**Three defensive layers were found sitting ahead of `run()`'s own try/except (the one
WB-4 fixed)**:
1. `file_reader.read_jsonl` — per-line `except json.JSONDecodeError: continue`.
2. `stream_normalizer.StreamNormalizer._process_z` — wraps
   base64-decode/zlib-inflate/json.loads in its own `except Exception: return []`.
3. `message_bus.SessionMessageBus.emit` — wraps every handler call in
   `except Exception: logger.exception(...)`.

Only **one** of the 12 fixtures reaches past all three and genuinely exercises WB-4's
catch/raise: `non_utf8_bytes.jsonl`. Its invalid UTF-8 bytes break `TextIOWrapper`
decoding inside `f.readline()` — a `UnicodeDecodeError`, not caught by layer 1's
`JSONDecodeError`-only handler, and outside `emit()` so layer 3 never applies either.
This is the first real (non-mocked) evidence that WB-4's fix holds against actual
corrupted data, not just an injected `RuntimeError`.

## Sanity check — the test can fail

Before trusting `test_non_utf8_bytes_raises_and_marks_build_failed`, WB-4's `raise`
statement was temporarily removed from `preprocessor.py` (reverting to the pre-fix
swallow-and-return behaviour) and the test re-run in isolation:

```
FAILED tests/integration/test_wb4_malformed_jsonl_fixtures.py::Wb4MalformedJsonlFixtures::test_non_utf8_bytes_raises_and_marks_build_failed
AssertionError: None is not None : non_utf8_bytes.jsonl must propagate an exception out of run() -- ...
```

The change was reverted immediately after (`git checkout -- app/processing/preprocessor.py`).
This confirms the test is a real gate on this exact behaviour, not a rubber stamp.

## Acceptance-criteria-to-test mapping (fixture → outcome)

| Fixture | Outcome | Test |
|---|---|---|
| `non_utf8_bytes.jsonl` | **Raises `UnicodeDecodeError`, `failed=True`, `status=error`** — the one real (non-mocked) confirmation of WB-4's contract | `test_non_utf8_bytes_raises_and_marks_build_failed` |
| `truncated_final_line.jsonl` | Dropped at read_jsonl (JSONDecodeError), build completes, surrounding lines processed | `test_truncated_final_line_is_dropped_not_fatal` |
| `missing_type_field.jsonl` | Processed as an unrouted `""`-topic message, not fatal | `test_missing_type_field_is_processed_as_unrouted_topic_not_fatal` |
| `invalid_base64_z_payload.jsonl` | Dropped at normalizer `_process_z`, build completes | `test_invalid_base64_z_payload_entry_is_dropped_not_fatal` |
| `invalid_zlib_z_payload.jsonl` | Dropped at normalizer `_process_z`, build completes | `test_invalid_zlib_z_payload_entry_is_dropped_not_fatal` |
| `z_payload_decompresses_to_non_json.jsonl` | Dropped at normalizer `_process_z` (json.loads on decompressed text fails) | `test_z_payload_decompresses_to_non_json_entry_is_dropped_not_fatal` |
| `unparseable_datetime.jsonl` | Dropped at read_jsonl (`_parse_timestamp` returns None) | `test_unparseable_datetime_entries_are_dropped_not_fatal` |
| `embedded_null_byte.jsonl` | Dropped at read_jsonl (JSONDecodeError: invalid control character) | `test_embedded_null_byte_line_is_dropped_not_fatal` |
| `empty_file.jsonl` | Degenerate but valid ("complete", 0 own messages) | `test_empty_file_builds_degenerate_but_valid` |
| `single_truncated_line.jsonl` | Degenerate but valid | `test_single_truncated_line_builds_degenerate_but_valid` |
| `corrupted_line_in_middle.jsonl` | Single glitch dropped, 7 surrounding good lines all still processed | `test_corrupted_line_in_middle_is_dropped_not_fatal` |
| `mixed_line_endings.jsonl` | Not actually malformed to the parser (universal newlines) — all lines processed | `test_mixed_line_endings_all_lines_processed_not_fatal` |

Plus one structural-finding test, not itself a WB-4 assertion:
`test_fixture_as_provided_never_opens_the_sessioninfo_gate`.

## Finding for follow-up (not fixed here — out of test-engineer scope)

None of the 11 "handled gracefully" fixtures represent a WB-4 gap — each is
individually swallowed by design at a layer WB-4 doesn't own. **No gap was found**
where a fixture silently produced a "successful" build despite genuine corruption that
should have failed it. The one fixture that does exercise WB-4's own contract
(`non_utf8_bytes.jsonl`) confirms the fix holds for real corrupted data, not only the
mocked injection.

One adjacent, pre-existing observation worth a human's attention, unrelated to WB-4:
`message_bus.SessionMessageBus.emit` (layer 3 above) swallows **every** processor
exception individually and only logs it — meaning a processor bug on a single
message can never propagate to `run()` at all, regardless of WB-4's fix. That may be
entirely intentional (processor isolation), but it does mean WB-4's `raise` mainly
covers `preprocessor.py`'s own bookkeeping/file-I/O code, not processor bugs
downstream of `emit()`. Flagging for awareness, not proposing a change.

## Command

```
venv/bin/python -m pytest tests/integration/test_wb4_malformed_jsonl_fixtures.py -v
```

**Result**: 13/13 passed.

## Coverage note

No coverage tooling is configured in this project yet (`.claude/project-commands.json`
does not exist; per `CLAUDE.local.md` "Known Issues", WB-1 covers standing this up).
Coverage gates cannot be measured for this change; noted explicitly per
`.claude/rules/testing.md` ("When coverage cannot be measured") rather than omitted.
Full existing suite re-run for regressions: `venv/bin/python -m pytest -q` →
207 passed, 2 failed (both pre-existing on this branch, confirmed via `git stash` —
`tests/test_router_livetiming_stream_http.py`, unrelated `FakeEngine.add_client()`
signature drift, nothing to do with this change).
