"""Test-suite isolation: redirect the app's data home to a throwaway tempdir.

Python imports this package before any `tests.test_*` module, and therefore
before any `app.*` import — which matters because `app.settings.DATA_HOME` (and
with it `config.TMP_DIR`, `CACHE_DIR` and `SETTINGS_FILE`) is resolved once at
import time. Setting `F1_DATA_HOME` here makes the whole suite read and write a
temp directory instead of the live data home; the env var takes precedence over
`instance.env`, so it wins on a configured checkout too.

Without it, tests that build transient DBs through `SessionPreProcessor` /
`transient_db_path()` leak scratch `.db` files into the real `DATA_HOME/tmp`
(~70 had accumulated in `devData/tmp`). Card 6a63bea1.

Covers `python -m unittest discover -s tests` (CI) and pytest alike. Running a
test file directly (`python tests/test_x.py`) bypasses the package import and
so is NOT isolated — run the suite instead.
"""
import atexit
import os
import shutil
import tempfile

_TEST_DATA_HOME = tempfile.mkdtemp(prefix="f1u-tests-")
os.environ["F1_DATA_HOME"] = _TEST_DATA_HOME
atexit.register(shutil.rmtree, _TEST_DATA_HOME, ignore_errors=True)
