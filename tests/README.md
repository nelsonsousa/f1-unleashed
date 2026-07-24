# Tests

Skeleton test layout, inherited by every project. Fill in once the tech stack
is chosen (workflow phase 3), then wire the runners into `.github/workflows/ci.yml`.

- `unit/` — fast, isolated, no I/O. Run on every push. One component under test.
- `integration/` — cross-component / real dependencies. Run in the `test` branch.

Both suites run in CI. Locally, run them in the `test` branch only (per the
git workflow in CLAUDE.md).

## Running

```bash
python -m unittest discover -s tests -t . -p "test_*.py"   # Python
node --test tests/*.mjs                                    # frontend
```

**Keep the `-t .`.** It makes `tests` import as a package, so `tests/__init__.py`
runs first and points `F1_DATA_HOME` at a throwaway tempdir. Without it, unittest
imports the test modules top-level, the fixture never runs, and the DB-building
tests leave scratch `.db` files in the real data home (card 6a63bea1). pytest
picks the package up on its own.
