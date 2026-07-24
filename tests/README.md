# Tests

Skeleton test layout, inherited by every project. Fill in once the tech stack
is chosen (workflow phase 3), then wire the runners into `.github/workflows/ci.yml`.

- `unit/` — fast, isolated, no I/O. Run on every push. One component under test.
- `integration/` — cross-component / real dependencies. Run in the `test` branch.

Both suites run in CI. Locally, run them in the `test` branch only (per the
git workflow in CLAUDE.md).
