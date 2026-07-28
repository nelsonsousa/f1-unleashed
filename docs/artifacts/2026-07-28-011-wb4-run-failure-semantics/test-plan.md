---
task: WB-4 — `SessionPreProcessor.run()` failure-semantics fix + regression tests (R1)
kind: bug fix (Red Gate)
basis:
  - docs/artifacts/2026-07-27-006-backend-review-replan/backend-synthesis.md §1.4 (R1), §5.3
  - docs/artifacts/2026-07-27-007-project-kickoff/test-battery-plan.md §5 (WB-4), §6.2
status: RED — confirmed failing against unfixed code, for the right reason
---

# Test plan — WB-4: `run()` swallowed-failure semantics

## Bug

`SessionPreProcessor.run()` (`app/processing/preprocessor.py:498-504`) catches every
internal exception in a single blanket `except Exception:`, sets `self.failed = True`,
logs, and **returns normally** — it never re-raises. Four call sites exist:

| Caller | Checks `.failed`? |
|---|---|
| `SessionEngine._run_preprocess` (`app/processing/session.py`) | **Yes** — pinned by `tests/test_preprocess_failure_surfaced.py` |
| `LiveTimingFetcher.fetch_session` (`app/services/livetiming_fetcher.py:758-771`) | **No** |
| `live_capture._capture_loop` (`app/services/live_capture.py:419-427`) | **No** |
| `utils/scripts/reprocess_year.py` (+ `reprocess_all.py`) | **No** |

Three of four callers treat "`run()` didn't raise" as "the build succeeded".

## Target call site for this regression test

**`LiveTimingFetcher.fetch_session`**, not `reprocess_year.py` as originally suggested
by test-battery-plan.md §6.2's example.

**Why the plan's suggested target was not used**: `utils/scripts/reprocess_year.py`
currently cannot be imported at all — line 32, `from app.services.cache_manager import
cache_manager`, references a module that does not exist anywhere in this codebase
(confirmed via `grep -rn cache_manager --include="*.py" .`, only hits are in that file
itself). That is a separate, pre-existing defect, out of WB-4's scope (R1 is about
`run()`'s failure semantics, not about a broken import), and not something this task
touches per the instruction not to modify caller files. `fetch_session` is one of the
same three non-checking callers named directly in R1, is fully importable, and this
project already has an established test pattern for driving it with HTTP stubbed
(`tests/test_download_post_build_call.py`), which this test follows.

## Injection mechanism

`file_reader.read_jsonl` already swallows malformed-JSON lines internally
(`json.JSONDecodeError` → `continue`, `file_reader.py:185-188`), so a malformed
`.jsonl` line does **not** reach `run()`'s exception handler — it was ruled out as an
injection point after reading the code. Instead, `SessionMessageBus.emit` — the call
every processed message is routed through (`preprocessor.py:385`, `:446`) — is
monkeypatched to raise a `RuntimeError` for one topic (`TrackStatus`) partway through
an otherwise normal two-message build (`SessionInfo` opens the gate, `TrackStatus`
arrives after). This reproduces the real shape of the bug: an exception from deep
inside message processing, caught by `run()`'s blanket handler, silently swallowed.
`SessionPreProcessor` itself is **not** mocked away — the real class executes against
real fixture data, so the swallow-and-continue mechanism is genuinely exercised.

## Acceptance criterion under test

> A caller of `SessionPreProcessor.run()` must not report success (or otherwise
> proceed as normal) for a build that failed partway through.

## Red Gate

**Test file**: `tests/test_wb4_preprocessor_run_failure_semantics.py`
**Test**: `Wb4LivetimingFetcherRunFailureSemantics.test_fetch_session_reports_success_for_a_build_that_actually_failed`

**Command**: `./venv/bin/python -m unittest tests.test_wb4_preprocessor_run_failure_semantics -v`

**Result against unfixed code (confirmed 3x for non-flakiness)**: **FAIL**, as expected.

```
Pre-processing error: 11330_Qualifying
Traceback (most recent call last):
  File ".../app/processing/preprocessor.py", line 446, in run
    self._bus.emit(filtered.topic, filtered.data, filtered.timestamp)
  File ".../tests/test_wb4_preprocessor_run_failure_semantics.py", line 65, in _emit_that_fails_on_track_status
    raise RuntimeError("simulated processor failure mid-run (WB-4 injection)")
RuntimeError: simulated processor failure mid-run (WB-4 injection)
FAIL

======================================================================
FAIL: test_fetch_session_reports_success_for_a_build_that_actually_failed
----------------------------------------------------------------------
Traceback (most recent call last):
  File ".../tests/test_wb4_preprocessor_run_failure_semantics.py", line 138, in test_fetch_session_reports_success_for_a_build_that_actually_failed
    with self.assertRaises(
        Exception, ...
    ):
AssertionError: Exception not raised : fetch_session() must not return normally when
the underlying build failed partway through run() -- it did today, silently reporting
the session as usable

----------------------------------------------------------------------
Ran 1 test in 0.037s

FAILED (failures=1)
```

**Why this is a true red gate, not an incidental failure**: the traceback shows the
injected `RuntimeError` was raised inside `run()`'s real message-processing loop,
caught by `run()`'s own `except Exception:` (visible as the "Pre-processing error"
log line + full traceback from `logger.exception`), and `fetch_session()` then
returned normally — the `assertRaises` failed because **no exception reached the
test**, which is exactly the bug: the caller cannot tell a failed build from a
successful one. This is not an import error, a fixture typo, or an unrelated crash —
it fails for the precise reason R1 describes.

**Sanity assertion (would run after the `assertRaises` block, not currently
reached because the block itself fails first)**: `captured["pre"].failed is True` —
confirms the injected exception actually reached `run()`'s swallow path, so a future
green run is attributable to the fix, not to the injection failing to fire. Verified
manually via an ad hoc script during development (deleted before commit): with the
injection but no fix, `fetch_session()` returns `cache_dir` normally and
`pre.failed` is `True`.

## Design note for the implementer (not a decision made here)

R1's own disposition text (`backend-synthesis.md` §1.4) offers two directions: "raise
by default, or return a typed result the caller cannot ignore." This test asserts the
**raise** contract (`assertRaises(Exception)` around `fetch_session(...)`), since that
is R1's first-listed option and gives the simplest, most conservative fix shape for a
function that currently returns a bare `Path`. If the implementer instead chooses a
typed-result design, this assertion will need a corresponding, explicit update as part
of that implementation — not a silent weakening. Flagging this now so it is a known,
open decision rather than a surprise at the green-gate stage.

## Acceptance-criteria-to-test mapping

| Criterion | Test |
|---|---|
| A build that fails partway through `run()` must not be reported as successful by a non-checking caller | `test_fetch_session_reports_success_for_a_build_that_actually_failed` |

## Edge cases and negative paths not covered by this red-gate test (left for the fix's own follow-on tests, per WB-4's green-gate phase)

- `live_capture._capture_loop`'s non-checking call site (R1 also names it) — not
  exercised here; `live_capture.py`'s capture loop is deeply coupled to SignalR/audio
  plumbing and is a poor target for an isolated red-gate test. Recommend a
  `live_capture`-specific regression test as a follow-up once the fix's contract
  (raise vs. typed result) is settled, so it can assert the same contract rather than
  inventing a second one.
- `SessionEngine._run_preprocess` (the one caller that already checks `.failed`) is
  already covered by `tests/test_preprocess_failure_surfaced.py` and is explicitly
  out of scope here — it is not part of the bug.
- `utils/scripts/reprocess_year.py` / `reprocess_all.py` — cannot be tested at all
  until the separate `cache_manager` import defect is fixed. Flagging as a
  **pre-existing, out-of-scope finding**: this script is currently unusable
  (`ModuleNotFoundError: No module named 'app.services.cache_manager'` on `import`),
  independent of WB-4.

## Coverage note

No coverage tooling is configured in this project yet (`.claude/project-commands.json`:
`coverage`/`patch_coverage` are both `TODO`, pending WB-1). Coverage gates cannot be
measured for this change; noted explicitly per `.claude/rules/testing.md` ("When
coverage cannot be measured") rather than omitted.
