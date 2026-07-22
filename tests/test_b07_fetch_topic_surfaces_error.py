"""B07 vIRYhvAg — _fetch_topic_data must SURFACE a real fetch error (network / 5xx
/ 403 WAF) by RAISING, not swallow it as an empty topic.

A swallowed error made the download report the topic as "done (0 messages)" and go
silently missing. The fix re-raises aiohttp.ClientError; a legit 404 (the topic
genuinely isn't in the session) still returns [] and is NOT treated as an error.

Regression guard for vIRYhvAg — a reversion to `return []` in the except would fail
the first two tests.
"""
import asyncio
import unittest
from unittest import mock

import aiohttp

from app.services.livetiming_fetcher import LiveTimingFetcher


class _FakeCM:
    """The async context manager session.get() returns. __aenter__ either yields a
    fake response or raises, to simulate a connection failure."""

    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeResp:
    def __init__(self, status):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=mock.Mock(), history=(), status=self.status)

    async def text(self):
        return ""


def _fetcher(cm):
    f = LiveTimingFetcher.__new__(LiveTimingFetcher)   # bypass __init__
    f._get_session = mock.AsyncMock(
        return_value=mock.Mock(get=mock.Mock(return_value=cm)))
    return f


class TestFetchTopicSurfacesError(unittest.TestCase):
    def test_connection_error_raises_not_swallowed(self):
        cm = _FakeCM(raise_exc=aiohttp.ClientConnectionError("boom"))
        f = _fetcher(cm)
        with self.assertRaises(aiohttp.ClientError):
            asyncio.run(f._fetch_topic_data("http://x/", "TimingData"))

    def test_5xx_raises_not_swallowed(self):
        cm = _FakeCM(resp=_FakeResp(500))
        f = _fetcher(cm)
        with self.assertRaises(aiohttp.ClientError):
            asyncio.run(f._fetch_topic_data("http://x/", "TimingData"))

    def test_404_returns_empty(self):
        # A legit 404 is NOT an error — the topic simply isn't in this session.
        cm = _FakeCM(resp=_FakeResp(404))
        f = _fetcher(cm)
        result = asyncio.run(f._fetch_topic_data("http://x/", "TimingData"))
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
