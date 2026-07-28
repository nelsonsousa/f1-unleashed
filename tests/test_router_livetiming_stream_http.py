"""WB-7 — HTTP-level (WebSocket) contract tests for
app/routers/livetiming_stream.py.

The real SessionEngine needs a live SignalR connection or a populated
session cache dir + preprocessor pass to construct — not something to
fake convincingly at the HTTP layer without an integration-style fixture
(out of scope for this pass; noted in test-plan.md). Instead we mock
`session_manager` (the module-level singleton the router imports) with a
lightweight fake engine, and exercise the router's own connect/command/
error-handling logic — which is the part that is actually router
behavior, as opposed to SessionEngine's.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
import app.routers.livetiming_stream as stream_router


class FakeEngine:
    def __init__(self):
        self.commands = []
        self.client_id = 7

    async def add_client(self, ws):
        return self.client_id

    async def handle_command(self, cmd):
        self.commands.append(cmd)

    def remove_client(self, client_id):
        pass


class WebsocketSessionEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_connect_and_forward_command_to_engine(self):
        fake_engine = FakeEngine()
        with mock.patch.object(stream_router.session_manager, "get_or_create",
                                mock.AsyncMock(return_value=fake_engine)):
            with self.client.websocket_connect("/api/v1/livetiming/ws/2026_Spa_Race") as ws:
                ws.send_json({"cmd": "play"})
                ws.close()
        self.assertEqual(len(fake_engine.commands), 1)
        self.assertEqual(fake_engine.commands[0]["cmd"], "play")
        # The router stamps the originating websocket onto the command so the
        # engine can address per-client replies.
        self.assertIn("_ws", fake_engine.commands[0])

    def test_non_object_command_is_ignored_not_fatal(self):
        fake_engine = FakeEngine()
        with mock.patch.object(stream_router.session_manager, "get_or_create",
                                mock.AsyncMock(return_value=fake_engine)):
            with self.client.websocket_connect("/api/v1/livetiming/ws/2026_Spa_Race") as ws:
                ws.send_text("[1, 2, 3]")   # valid JSON, but not an object
                ws.send_json({"cmd": "pause"})
                ws.close()
        # The list command must be dropped silently; only the valid one reaches the engine.
        self.assertEqual(len(fake_engine.commands), 1)
        self.assertEqual(fake_engine.commands[0]["cmd"], "pause")

    def test_unknown_session_sends_error_topic_and_closes(self):
        with mock.patch.object(stream_router.session_manager, "get_or_create",
                                mock.AsyncMock(side_effect=ValueError("Session not found: nope"))):
            with self.client.websocket_connect("/api/v1/livetiming/ws/nope") as ws:
                msg = ws.receive_json()
        self.assertEqual(msg["topic"], "error")
        self.assertIn("Session not found", msg["data"]["message"])


if __name__ == "__main__":
    unittest.main()
