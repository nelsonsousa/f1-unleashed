"""WB-11 (Trello YVCb5HAF) — e2e connectivity smoke test: the app's real
`F1SignalRClient` against the real sim (repos/sim), over a real websocket,
inside the `e2e` docker-compose profile.

This is deliberately ONE test demonstrating the pattern the card asked for
("test end-to-end against the live replica"), not a full e2e suite — see
docker/README.md for what that scope decision means and what's left.

What it proves, for real, not by inference:
  - the sim's negotiate + websocket handshake is reachable and speaks the
    protocol `F1SignalRClient` expects (the same class `app/services/
    signalr_client.py` uses for the real live connection — this test does
    not reimplement or mock any of that logic)
  - a `Subscribe` round-trip completes and real messages flow back
  - the messages are the actual fixture content (topics, terminal
    SessionStatus), not just "something arrived"

It intentionally does NOT boot the full FastAPI app or a browser — this
project has no browser test harness yet (see docker/README.md, "Not
covered"), and driving the app's HTTP/websocket layer end-to-end doesn't
exercise anything `F1SignalRClient` itself doesn't already cover for the
one thing this card is actually about: real network connectivity to a
live-shaped feed.

Run: only meaningful inside the `e2e` compose profile (needs a reachable
`sim` service + SIM_BASE_URL). Skips itself everywhere else, including a
plain host `pytest` run, so it never pollutes the normal unit/integration
gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

SIM_BASE_URL = os.environ.get("SIM_BASE_URL")

pytestmark = pytest.mark.skipif(
    not SIM_BASE_URL,
    reason=(
        "SIM_BASE_URL not set -- this test only runs inside the docker-compose "
        "`e2e` profile (docker/README.md), never as part of a normal pytest run."
    ),
)

FIXTURE_SESSION_SUFFIX = "9999_Docker-Smoke-CDN/1_Practice"
CONNECT_TIMEOUT_SECONDS = 30


def test_app_client_connects_to_sim_and_captures_real_messages():
    import requests

    # 1. Point the app's settings at the sim, the same way a human would via
    #    the settings dialog (endpoints.f1LivetimingBase) -- see repos/sim's
    #    README, "Pointing the F1Unleashed client at the sim". Must happen
    #    before the first app.settings.get() call (constructing the client
    #    below), since settings are loaded+cached lazily on first access.
    data_home = Path(os.environ.get("F1_DATA_HOME", "/tmp/f1unleashed-e2e-data"))
    data_home.mkdir(parents=True, exist_ok=True)
    (data_home / "settings.json").write_text(
        json.dumps({"endpoints": {"f1LivetimingBase": SIM_BASE_URL}})
    )

    # 2. Confirm the sim is up and knows about our fixture session.
    sessions = requests.get(f"{SIM_BASE_URL}/api/sessions", timeout=10).json()
    assert sessions, (
        "sim reports zero sessions -- is docker/e2e-fixtures mounted at "
        "SIM_CDN_DATA_ROOT? (see docker-compose.test.yml, service `sim`)"
    )
    target = next(
        (s for s in sessions if s["path"].endswith(FIXTURE_SESSION_SUFFIX)), None
    )
    assert target is not None, (
        f"fixture session ending in {FIXTURE_SESSION_SUFFIX!r} not found "
        f"among sim sessions: {[s['path'] for s in sessions]}"
    )

    # 3. Configure the broadcast but do NOT start it yet -- start_at=None
    #    just loads the session. If the sim starts broadcasting before our
    #    client has finished the negotiate+websocket handshake, the earliest
    #    fixture messages are gone for good (this is a real live-style feed:
    #    new clients get what's broadcast from here forward, not history).
    #    Confirmed by first running this test with start_at="now" -- it
    #    connected and received messages, but the first 1-2 (TrackStatus,
    #    WeatherData) were already gone, which is exactly the race a
    #    connect-before-start ordering below eliminates.
    configure = requests.post(
        f"{SIM_BASE_URL}/api/broadcast/configure",
        json={"session_path": target["path"], "speed": 8.0},
        timeout=10,
    )
    configure.raise_for_status()

    # 4. Import only now -- app.settings.load() caches on first get(), and
    #    that first call must see the settings.json written in step 1.
    from app.services.signalr_client import F1SignalRClient

    cache_dir = data_home / "smoke_cache"
    client = F1SignalRClient(cache_path=cache_dir, no_auth=True, timeout=0)

    async def _drain():
        loop = asyncio.get_event_loop()
        queue = client.start(loop=loop)
        connected = False
        started_broadcast = False
        timing_messages = 0
        topics_seen: set[str] = set()
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
                if item.get("type") == "status" and item.get("status") == "connected":
                    connected = True
                    if not started_broadcast:
                        # Only now -- client is negotiated and subscribed --
                        # tell the sim to start replaying the fixture, so
                        # nothing is missed to the connect-time race above.
                        requests.post(f"{SIM_BASE_URL}/api/broadcast/start", timeout=10).raise_for_status()
                        started_broadcast = True
                if item.get("type") == "timing":
                    timing_messages += 1
                    topics_seen.add(item["topic"])
                    data = item.get("data")
                    if (
                        item["topic"] == "SessionStatus"
                        and isinstance(data, dict)
                        and data.get("Status") == "Ends"
                    ):
                        break  # real terminal marker from the fixture -- done
        finally:
            client.stop()
        return connected, timing_messages, topics_seen

    connected, timing_messages, topics_seen = asyncio.run(_drain())

    # 5. Assert on real, specific correctness -- not just "something arrived".
    assert connected, "F1SignalRClient never reported a `connected` status from the sim"
    assert timing_messages >= 4, (
        f"expected at least the 4 non-terminal fixture messages, got {timing_messages} "
        f"(topics seen: {sorted(topics_seen)})"
    )
    assert "TrackStatus" in topics_seen
    assert "WeatherData" in topics_seen
    assert "TimingData" in topics_seen
    assert "SessionStatus" in topics_seen

    # 6. The client also writes what it received to cache_path/live.jsonl --
    #    confirm that's real too, not just what passed through the queue.
    live_log = cache_dir / "live.jsonl"
    assert live_log.exists(), "F1SignalRClient did not write live.jsonl to cache_path"
    lines = [json.loads(l) for l in live_log.read_text().splitlines() if l.strip()]
    logged_topics = {l["Type"] for l in lines}
    assert "TrackStatus" in logged_topics
    assert "SessionStatus" in logged_topics
