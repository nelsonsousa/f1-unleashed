"""Regression (Trello UCzE3tB7 / WB-36): utils/scripts/reprocess_year.py imported
`app.services.cache_manager`, a module deleted in commit 6baee04 ("prune the
confirmed-dead code inventory") because it had no remaining callers at the time.
`reprocess_year.py` (and, transitively, `reprocess_all.py`, which imports
`reprocess_year` from it) was missed by that prune's "no dangling refs" check and
was left unimportable: `ModuleNotFoundError: No module named
'app.services.cache_manager'`.

The import (and the `cache_manager.cache_dir = CACHE_DIR` assignment that followed
it) was dead even before the module was deleted -- the script already builds every
path directly off the `CACHE_DIR` constant imported from `app.config`, so nothing in
the script's own logic depended on `cache_manager`. The fix removes the stale import
rather than reintroducing the deleted module.
"""
import importlib
import unittest


class ReprocessYearImport(unittest.TestCase):
    def test_reprocess_year_module_imports_cleanly(self):
        module = importlib.import_module("utils.scripts.reprocess_year")
        self.assertTrue(callable(module.reprocess_year))
        self.assertTrue(callable(module.main))

    def test_reprocess_all_module_imports_cleanly(self):
        # Transitively exercises the same import chain: reprocess_all.py imports
        # reprocess_year() from reprocess_year.py.
        module = importlib.import_module("utils.scripts.reprocess_all")
        self.assertTrue(callable(module.reprocess_all))


if __name__ == "__main__":
    unittest.main()
