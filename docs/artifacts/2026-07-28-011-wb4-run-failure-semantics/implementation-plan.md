---
task: WB-4 — `SessionPreProcessor.run()` failure-semantics fix (R1)
kind: bug fix (Implementation)
basis:
  - docs/artifacts/2026-07-28-011-wb4-run-failure-semantics/test-plan.md
  - tests/test_wb4_preprocessor_run_failure_semantics.py (Red Gate, confirmed failing)
  - tests/test_preprocess_failure_surfaced.py (existing passing test — must not regress)
---

# Implementation plan — WB-4: `run()` swallowed-failure semantics

## Goal

Make `SessionPreProcessor.run()` re-raise the internal exception it currently
swallows, so a caller that does not explicitly check `.failed` cannot mistake
a failed build for a successful one. Satisfy
`tests/test_wb4_preprocessor_run_failure_semantics.py` without regressing any
of the other 70 currently-passing tests, in particular
`tests/test_preprocess_failure_surfaced.py`.

## Minimal-change strategy

Option (a) from the test-plan's design note: **re-raise from `run()`** rather
than introducing a typed result object. This is the smaller of the two
options, matches the contract the Red Gate test already asserts
(`assertRaises(Exception)` around `fetch_session(...)`), and does not touch
any caller's public signature.

`run()`'s blanket `except Exception:` block (preprocessor.py:501-504) today:
1. Logs the exception (`logger.exception`)
2. Sets DB meta status to "error"
3. Sets `self.failed = True`
4. Returns normally (implicit)

Change: keep steps 1–3 exactly as they are (so `.failed` and DB status remain
correct for the one caller that already checks `.failed`,
`SessionEngine._run_preprocess`), then re-raise the caught exception instead
of falling through to a normal return.

## Files in scope

1. `app/processing/preprocessor.py` — `SessionPreProcessor.run()`, the
   `except Exception:` block only (lines ~501-504). Add `raise` after setting
   `self.failed = True`.

## Files investigated, not touched

- `app/processing/session.py` (`SessionEngine._run_preprocess`) — already
  wraps `await self._preprocessor.run(...)` in its own
  `try/except Exception as e: ... finally: ...` that both (a) catches a raised
  exception and records `_preprocess_error`, and (b) separately checks
  `self._preprocessor.failed` in the `finally` block as a fallback. This
  caller is correct under both the old (swallow) and new (raise) semantics of
  `run()` — no change needed. Confirmed no regression by re-running
  `tests/test_preprocess_failure_surfaced.py`, which drives `_run_preprocess`
  with a mocked preprocessor (`side_effect=...` / `raises=...`) independent of
  `run()`'s real implementation, so it is unaffected by this change either way.

- `app/services/livetiming_fetcher.py` (`LiveTimingFetcher.fetch_session`) —
  the call site the Red Gate test targets. `await pre.run(force=True)` at
  line 766 sits inside a bare `try: ... finally: pre.close()` with **no**
  `except` clause — so once `run()` raises, the exception already propagates
  naturally out of `fetch_session()` after `pre.close()` runs. No code change
  needed here: this caller was only "non-compliant" because `run()` itself
  never raised anything to propagate. Verified by re-running the Red Gate
  test after the `preprocessor.py` change.

- `app/services/live_capture.py` (`_capture_loop`) — **explicitly out of
  scope for this pass.** Not covered by the Red Gate test, and the test-plan
  flags it as needing a dedicated regression test once the fix's contract is
  settled (deeply coupled to SignalR/audio plumbing, a poor target for an
  isolated test here). Left untouched. **Follow-up work required**: this
  caller does not check `.failed` and, after this fix, `run()` raising into
  `_capture_loop` may propagate somewhere unhandled — needs its own
  investigation and test, not a speculative touch in this task.

- `utils/scripts/reprocess_year.py` / `reprocess_all.py` — **explicitly out
  of scope for this pass.** Pre-existing, unrelated defect
  (`ModuleNotFoundError: app.services.cache_manager`) makes these scripts
  unimportable today, independent of WB-4. Left untouched. **Follow-up work
  required**: once the import defect is fixed separately, these scripts still
  do not check `.failed` / catch a raised exception from `run()` and will
  need the same treatment as `_capture_loop`.

## Ordered steps

1. Edit `app/processing/preprocessor.py`: add `raise` at the end of `run()`'s
   `except Exception:` block, after `self.failed = True`.
2. Run `tests/test_wb4_preprocessor_run_failure_semantics.py` — confirm PASS.
3. Run `tests/test_preprocess_failure_surfaced.py` — confirm still PASS (no
   regression).
4. Run the full suite — confirm 71/71 (or more) passing.

## Risks

- Any other caller of `run()` that today relies on it *never* raising and
  does not catch `asyncio.CancelledError`/`Exception` around it could see a
  new unhandled exception. Audited all four known call sites above; only
  `live_capture.py` and the `reprocess_*.py` scripts are unhandled, and both
  are explicitly flagged as follow-up rather than silently fixed or silently
  ignored.
- `run()` already re-raises `asyncio.CancelledError` in a separate `except`
  clause above the general `except Exception:` — unaffected by this change.

## Rollback approach

Single-line revert: remove the added `raise` statement in
`app/processing/preprocessor.py`. No schema, API, or signature changes to
undo elsewhere.
