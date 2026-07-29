# Implementation-time decisions — branch `feat/wb5-6-pipeline-redesign`

This branch implements the closed, human-authorized pipeline redesign
(`docs/artifacts/2026-07-27-003-pipeline-redesign/requirement-spec.md` §9,
`architecture-plan.md`): a new `StreamNormalizer` module, the `file_reader.py`
rewrite to a pure tail-follow reader, `preprocessor.py`'s rewiring to use the
normalizer, and the D7-B `telemetry_processor.py` pairing fix.

Per the human's explicit instruction ("give it a go on a separate branch...
document the decisions so they can be reverted later if needed"), every
judgment call made where the closed design under-specifies an
implementation detail is recorded below, each written so it can be reverted
independently without touching the others.

---

## 1. The universal 60-minute-before-scheduled-start gate (§9.2) is NOT wired in this pass — the old `SessionInfo.Key` gate stays

**What the closed design says:** replace `preprocessor.py`'s `SessionInfo.Key`
gate, `_gate_buffer`, `_expected_key`, and the old `.z` pre-start skip with
one universal rule: discard any message whose payload timestamp is >60
minutes before the session's *scheduled* start time (`requirement-spec.md`
§9.2). `StreamNormalizer` would own this gate directly.

**What I implemented instead:** `StreamNormalizer` fully implements the
universal gate (`_gate()`, `GATE_WINDOW`, `scheduled_start_utc` constructor
param) and it is unit-tested (`tests/unit/test_stream_normalizer.py`,
`UniversalGate` test class) — but `preprocessor.py` constructs
`StreamNormalizer()` **without** passing `scheduled_start_utc`, so the gate
is a no-op today (see `_gate()`'s `if self._gate_cutoff is None: return True`
short-circuit). The old `SessionInfo.Key` gate (`_gated`, `_gate_buffer`,
`_expected_key`, `GATE_TIMEOUT_S`) is **unchanged** and still does the job of
deciding when the session "starts" and anchoring `_start_time`/the
normalizer's `reference_utc`. The old pre-start `.z` skip
(`if self._start_time is None and msg.topic in ("CarData.z", "Position.z")`)
is also **unchanged**, still guarding the confirmed zombie-message bug
(`CVFyRpfx`) exactly as it does today.

**Why:** `architecture-plan.md` §A.9.3 itself flags the wiring for this as
unresolved ("*(new dependency wiring)* ... small, exact site not yet
identified") and `on_baseline_ready`'s replacement trigger as "still open, not
resolved by this closure." Threading `scheduled_start_utc` into
`SessionPreProcessor` correctly means auditing every call site that
constructs it (`main.py`'s live-session monitor, replay/reprocess scripts,
routers) to source and pass the schedule — a genuinely cross-cutting change
outside this task's two named deliverables (StreamNormalizer + D7-B), and
risky to do quickly without dedicated design/test time of its own. Keeping
the old gate is the smaller, safer diff: it preserves the exact
`_start_time`/`offset_ms=0` anchor semantics every existing transient DB,
seek, and analysis output already depends on, while still delivering the
redesign's actual data-integrity value (trustworthy `utcTimestamp`, the
continuous dedup rule, D7-B) on top of it.

**Consequence:** the two guards §9.2 was designed to *replace* are still
doing their old job, side by side with the new normalizer. This is
redundant-but-safe, not broken — nothing regresses, the zombie-message case
is still caught by the pre-start `.z` skip exactly as before. What is
**not** yet true: the single-mechanism simplification the closed design
wanted (one gate, not two), and the corresponding removal of
`_gate_buffer`/`_expected_key`/the old cutoff filter's redundancy with the
universal gate.

**To complete this later (revert path — additive, not destructive):**
1. Determine the scheduled-start source for each `SessionPreProcessor(...)`
   call site (live: `main.py`'s schedule lookup; replay: session metadata
   already read for other purposes per `architecture-plan.md` §9.2's own
   note that this "is confirmed already available today").
2. Pass `scheduled_start_utc=` into `StreamNormalizer(...)` in
   `SessionPreProcessor.__init__`.
3. Remove `_gated`, `_gate_buffer`, `_gate_first_ts`, `_expected_key`,
   `GATE_TIMEOUT_S`, and the pre-start `.z` skip from `preprocessor.py`;
   remove the `SessionInfo`-triggered `set_reference()` call (the normalizer
   will auto-set its reference to the first gate-surviving message).
4. Re-specify `on_baseline_ready`'s trigger (flagged open by the
   architecture doc itself, independent of this decision).
5. Re-run the full regression corpus — row counts *will* shift (the
   universal gate changes which messages survive pre-session), which
   `architecture-plan.md` §A.7.7 already documents as an expected
   improvement, not a regression, once this lands.

**To revert this specific decision** (i.e. undo *this* branch's approach and
go back to relying purely on the old gate with no normalizer involvement at
all): revert `preprocessor.py`'s changes and `file_reader.py`'s changes,
delete `stream_normalizer.py` and its tests. Independent of decisions 2-5
below.

---

## 2. `StreamNormalizer.set_reference()` kept as an explicit override, not removed

**What the closed design says** (§9.2): "`REFERENCE_UTC_TIMESTAMP` is now
simplest of all: the first message to *survive* the universal gate" —
implying no external push is needed once the universal gate is wired
(decision #1).

**What I implemented:** `StreamNormalizer._stamp()` auto-sets
`reference_utc` to the first message it stamps if unset (matching §9.2's
"first survivor" semantics for a *future* build that wires the universal
gate). But because decision #1 keeps the old `SessionInfo.Key` gate as the
real trigger for "session start," `preprocessor.py` calls
`self._normalizer.set_reference(msg.utc_timestamp)` explicitly at the
moment the old gate opens, overriding whatever the normalizer auto-picked
during the (up to ~40-minute) pre-gate buffering phase.

**Why:** without this override, `reference_utc` would auto-set to the very
first line the normalizer ever sees (which can be tens of minutes before
the human-meaningful "session start"), reintroducing exactly the offset
origin collision D4 was created to prevent
(`architecture-plan.md` §A.4). `offset_ms` values for messages processed
*before* the override (buffered messages) are consequently meaningless and
are never used — the buffer-flush loop in `preprocessor.py` doesn't read
`buffered.offset_ms`.

**Revert:** once decision #1 is completed (universal gate wired at
construction), this explicit `set_reference()` call becomes unnecessary and
should be deleted — the normalizer's own auto-reference will already be
correct, because the universal gate will suppress pre-session lines by
construction rather than by a separate buffering scheme.

---

## 3. `StreamNormalizer`'s gate is a no-op when `scheduled_start_utc` is `None`

**Not something the closed design specifies either way** — §9.2 assumes the
schedule is always available (and confirms it structurally is, via
`main.py`'s existing lookup). I added an explicit `None`-means-no-op path
(`_gate()`: `if self._gate_cutoff is None: return True`) rather than
raising, so that:
- `StreamNormalizer` is usable standalone/in tests without a schedule
  (matching architecture's own requirement that it be "constructible and
  drivable without a DB," extended here to "without a schedule").
- Decision #1's deferral doesn't crash `preprocessor.py`, which currently
  never provides one.

**Revert:** trivial — this is a permissive default, not a behavior anything
depends on continuing. Once decision #1 wires a real schedule everywhere,
this branch is simply never taken in production; it can be left as a
defensive default or turned into a hard requirement (raise if `None`) at
that point, whichever the team prefers.

---

## 4. Cumulative-list dedup (§9.1) is scoped to the two named topics, not generic array-detection

**What §9.1 literally says:** "does its payload contain a multi-entry
array/indexed structure (the tell that this is a resent history, not a
fresh delta)? If so, [apply the key/index filter]" — phrased as a general
property-of-the-payload test, not a fixed topic list.

**What I implemented:** `CUMULATIVE_LIST_TOPICS = {"RaceControlMessages",
"SessionData"}` — a fixed set, matching `architecture-plan.md` §A.3.3's own
"Cumulative list" row (which names exactly these two topics) and
§9.7/D10's list. Any other topic, even if its payload happens to be
array-shaped, gets singleton (content-hash) dedup only.

**Why:** a generic "detect any indexed structure" rule would have to guess,
per topic, what counts as an entry's "key" (RCM/SessionData both nest their
arrays under a named field — `Messages`/`Series`/`StatusSeries` — with no
uniform schema across F1 topics). Getting this wrong for a topic nobody has
validated yet (§9.7/D10 flags `RaceControlMessages`/`SessionData`/
`TrackStatus` specifically as needing an SME-validated offsetMs-agreement
test before full confidence) risks silently corrupting a topic's data by
misapplying key-based filtering to something that isn't actually a resent
history. Scoping to the two topics the closed design itself names is the
narrower, safer, and literally-what-was-specified reading.

**Revert / extend:** to add a third topic (e.g. if `TrackStatus`'s
validation, per D10's outstanding obligation, later turns out to need the
same treatment), add it to `CUMULATIVE_LIST_TOPICS` and extend
`_entry_index_keys()` with its specific indexed-field name — a small,
additive, independently-revertible change per topic.

---

## 5. `SessionData`'s indexed section: `StatusSeries` preferred over `Series` when both present

**Not specified by the closed design** — §9.1's own proof case
(`requirement-spec.md` §9.1) uses `StatusSeries` exclusively; `Series`
follows an identical shape per the architecture doc but has no cited proof
case of its own.

**What I implemented:** `_entry_index_keys()` checks `StatusSeries` first,
falling back to `Series` only if `StatusSeries` is absent from that message.
A message carrying **both** in the same payload would have its `Series`
entries silently ignored for dedup purposes (they'd ride along unfiltered
inside whatever `_rebuild_cumulative_payload` reconstructs — actually: if
`StatusSeries` is present, the *entire* rebuilt payload is
`{"StatusSeries": kept}`, meaning any co-present `Series` data in the same
message would be dropped, not merely left unfiltered).

**Why accepted:** no fixture or SME-cited example currently shows a single
`SessionData` message with both sections populated simultaneously; treating
them as mutually exclusive per-message matches every example in the closed
design's own text. Flagged here specifically because it's the one place
this implementation could silently lose data if that assumption is wrong.

**Revert / fix:** if a real capture shows both sections co-present,
`_entry_index_keys()`/`_rebuild_cumulative_payload()` need to track and
filter each section independently (two watermarks per topic instead of
one) rather than picking one. This is a `test-engineer`/SME question to
settle empirically, per D10's own outstanding validation obligation
(`requirement-spec.md` §9.7) — not resolved by this branch.

---

## 6. `.z` topic dedup keys on the entry's own payload timestamp string, not a synthesized index

**What §9.1 says:** the continuous dedup rule "applies uniformly... and to
`.z` topics too" via the same key/index high-water-mark mechanism.

**What I implemented:** for `.z` topics, the "key" is the entry's own
payload timestamp string (`Utc`/`Timestamp`), compared lexicographically
(ISO-8601 strings sort correctly as timestamps). A resent `.z` burst's
entries are filtered by "already-seen-or-older timestamp," not by an
explicit array index (unlike RCM/SessionData, `.z` entries have no natural
integer key).

**Why:** `.z` entries don't carry an index/key the way RCM/SessionData's
`Messages`/`Series` dicts do — their only per-entry identity is their own
timestamp, which is exactly the property `split_z_entries` already extracts.
Using it as the high-water-mark key is the natural generalization of the
same rule, not a different one.

**Revert:** none needed — this is the direct, faithful application of
§9.1's rule to `.z`'s specific shape; there is no alternative reading of
the closed design this contradicts.

---

## 7. Branch base: `wb7-router-http-tests`, not one of the two named candidates

The task brief named `fix/wb8-capture-lifecycle` or `wb9-batch1-processor-coverage`
as the likely latest completed WB branch. Checking `git log -1 --format='%ci'`
across all sibling WB branches at task start:

| Branch | Last commit (local clock) |
|---|---|
| `fix/wb8-capture-lifecycle` | 2026-07-28 02:02:01 |
| `wb9-batch1-processor-coverage` | 2026-07-28 02:14:15 |
| `fix/wb4-preprocessor-run-failure-semantics` | 2026-07-28 08:17:28 |
| **`wb7-router-http-tests`** | **2026-07-28 08:17:52** (latest) |

`wb7-router-http-tests` is objectively the most recently committed of all
sibling WB branches (it also independently merges in `chore/wb1-test-tooling`,
so pytest/coverage tooling is already present). I branched from its tip
rather than the two named candidates, since the instruction was to use "the
most recently completed WB branch" by commit date and explicitly permitted
picking the actual latest if the two suggestions weren't it.

**Important gap this surfaces, not fixed by this branch:** none of the
sibling WB branches (`fix/wb2-nondeterministic-set-iteration`,
`wb3-telemetry-pairing-baseline`, `fix/wb4-preprocessor-run-failure-semantics`,
`wb7-router-http-tests`, `wb9-batch1-processor-coverage`,
`fix/wb8-capture-lifecycle`) have been merged into each other or into `dev` —
they are unmerged siblings, each branched from an earlier common ancestor.
`docs/artifacts/2026-07-27-007-project-kickoff/test-battery-plan.md` §3.1
explicitly states 003 (this redesign) should not be authorized for
implementation until WB-2 (determinism fix) and WB-3 (pairing-yield
baseline) have **landed** — meaning merged, not merely committed on their
own branch. This branch (`feat/wb5-6-pipeline-redesign`) does **not**
contain WB-2's determinism fix or WB-3's baseline-measurement commits,
because they live on branches this one wasn't built from. I proceeded
anyway per this task's explicit authorization ("WB-3 already measured
real telemetry pairing yield at 32.76%... already reviewed and confirmed
NOT to invalidate the closed architecture — proceed... do not re-derive"),
treating the *numbers* from WB-3 as given rather than needing the actual
WB-3 branch merged in. I did **not** independently re-verify WB-2's
determinism fix is present — this branch's own new tests do not depend on
the project-wide regression harness (`regression/regress.py`) being
trustworthy, since `tests/unit/test_stream_normalizer.py` and
`tests/regression/test_telemetry_pairing_yield_d7b.py` are self-contained
unit/regression tests with synthetic fixtures, not runs of that harness.

**Recommendation for whoever merges this:** before merging
`feat/wb5-6-pipeline-redesign` toward `dev`, first land WB-2
(`fix/wb2-nondeterministic-set-iteration`) and WB-4
(`fix/wb4-preprocessor-run-failure-semantics`, since it also touches
`preprocessor.py` and per `test-battery-plan.md` §3.3 should land before
003's own `preprocessor.py` changes to avoid two work blocks editing the
same control flow concurrently) — then rebase this branch on top and
re-run the full suite once more.

---

## 8. `tests/unit/`, `tests/integration/`, `tests/regression/` given `__init__.py`

**Not part of the redesign itself** — a build-time fix discovered while
adding this branch's own new tests. Without `__init__.py` in these three
(previously package-less) directories, pytest's default import mode
imports files under them as top-level modules rather than through the
`tests` package, which bypasses `tests/__init__.py`'s `F1_DATA_HOME`
redirect (`tests/README.md`'s own documented reason for requiring `-t .`
package-mode imports). Confirmed by reproduction: adding a new test file
under `tests/integration/` without an `__init__.py` present caused
cross-test failures in unrelated files (`tests/test_scan_time_bounds.py`,
`tests/test_preprocess_failure_surfaced.py`) that vanished once the three
`__init__.py` files were added — those tests read/wrote real filesystem
paths outside the isolated tempdir instead of the isolated one.

**This is a real, if narrow, fix — not scope creep on the redesign's own
account** — it was necessary to land this branch's own required test
deliverables at all under the existing (already-declared, WB-1-owned)
tooling. Flagged here rather than silently folded in, per the "don't
silently absorb extra work" rule; three empty `__init__.py` files, fully
reversible (`git rm tests/unit/__init__.py tests/integration/__init__.py
tests/regression/__init__.py`), no behavior change to any existing test.

---

## Explicitly out of scope for this branch (not decisions, just boundaries)

- **`signalr_client.py`** — confirmed zero changes needed, per §9.1's own
  finding that the marker design was dropped entirely. Not touched.
- **`clock.py`'s dead `display_delay_ms` (D6b)** — `requirement-spec.md`
  §9.4 states this sub-decision is "still open, not confirmed at closure."
  Not touched.
- **`on_baseline_ready`'s replacement trigger** — explicitly flagged as
  unresolved by the closed design itself (§9.2). Not touched; still fires
  at the old `SessionInfo.Key` gate-open, unchanged, consistent with
  decision #1 above.
- **D8 (high-reconnect-count flagging)** — the closed design defers the
  real fix to an existing carded bug, unrelated to this branch. Not
  touched.
- **Full live/CDN parity integration test
  (`tests/integration/test_live_cdn_parity.py`, AC-6′/AC-7′/AC-8)** — per
  `architecture-plan.md` §A.9.7, this needs `regression/golden/*` fixtures,
  which are not present in this worktree (they live at the project root,
  sibling to `repos/dev`, not inside this checkout, and are gitignored
  captured data, not committed source). Not built in this pass — the unit
  tests in `tests/unit/test_stream_normalizer.py` cover the same acceptance
  criteria against synthetic fixtures instead, per this project's "when you
  cannot test something, document it explicitly" rule
  (`rules/testing.md`).
