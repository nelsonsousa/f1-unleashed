"""Regression guard for RXblq6y1 — the download path's post-build call.

After writing live.jsonl, fetch_session eagerly builds the transient DB:

    await pre.run(force=True)

It used to pass `on_progress=...` as well, which `SessionPreProcessor.run()` has
never accepted. The ternary sat inside the argument VALUE, so the kwarg went in
unconditionally — every CDN download raised TypeError *after* the data had been
written correctly. The client was told the download failed and the eager build
never ran.

Nothing caught it because the defect is in the CALLER, and reaching that line
requires a network download. So this test drives the real `fetch_session` with
only the HTTP calls stubbed, and patches SessionPreProcessor with autospec=True
so the mock enforces run()'s true signature — a bad kwarg raises TypeError here
exactly as it did in production.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import livetiming_fetcher as lf
from app.services.livetiming_fetcher import LiveTimingFetcher, Meeting, Session


class DownloadPostBuildCall(unittest.IsolatedAsyncioTestCase):
    async def test_post_download_db_build_is_called_correctly(self):
        tmp = Path(tempfile.mkdtemp())
        fetcher = LiveTimingFetcher()
        fetcher.cache_dir = tmp

        meeting = Meeting(key=1, name="Test GP", official_name="Test GP",
                          location="Testville", country="Testland",
                          circuit="Test Circuit", number=1)
        session = Session(key=11, name="Practice 1", type="Practice",
                          path="2026/test/", start_date=None, end_date=None,
                          gmt_offset=None)

        with patch.object(lf.LiveTimingFetcher, "_fetch_json_file",
                          new=AsyncMock(return_value={})), \
             patch.object(lf.LiveTimingFetcher, "_fetch_topic_data",
                          new=AsyncMock(return_value=[])), \
             patch.object(lf, "SessionPreProcessor", autospec=True) as pre_cls:
            pre_cls.return_value.failed = False
            # Must not raise. Before the fix this died with
            # "run() got an unexpected keyword argument 'on_progress'".
            await fetcher.fetch_session(2026, meeting, session, force=True)

        pre_cls.return_value.run.assert_awaited_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
