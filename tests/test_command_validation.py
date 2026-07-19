"""B02 [P2] — WS command handler must validate command shape/arg types so a
malformed command is IGNORED, not allowed to raise (which tears down the whole
WebSocket via the router's generic handler). Pins what "right" is:

  - a non-object command (42, "play", [1]) is ignored, never raises;
  - a numeric arg of the wrong type (seek offset "abc", speed value "fast") is
    rejected and NOT forwarded to the downstream handler;
  - a well-formed command with correct types still dispatches unchanged.
"""
import unittest
from unittest import mock

from app.processing.session import SessionEngine


def _engine() -> SessionEngine:
    # bypass __init__ (spawns asyncio tasks); handle_command only needs the
    # downstream coroutines, which each test stubs out.
    return SessionEngine.__new__(SessionEngine)


class HandleCommandValidation(unittest.IsolatedAsyncioTestCase):
    async def test_non_object_command_is_ignored(self):
        e = _engine()
        # WRONG (before fix): cmd.get(...) on a non-dict raises AttributeError,
        # which escapes to the router and kills the socket. RIGHT: ignored.
        for bad in (42, "play", [1, 2], None):
            await e.handle_command(bad)

    async def test_non_numeric_seek_offset_is_rejected(self):
        e = _engine()
        e._seek = mock.AsyncMock()
        await e.handle_command({"cmd": "seek", "offset": "abc"})
        e._seek.assert_not_called()          # RIGHT: bad offset never reaches _seek

    async def test_valid_seek_offset_is_forwarded(self):
        e = _engine()
        e._seek = mock.AsyncMock()
        await e.handle_command({"cmd": "seek", "offset": 12.5})
        e._seek.assert_awaited_once_with(12.5)   # RIGHT: good command still works

    async def test_non_numeric_speed_is_rejected(self):
        e = _engine()
        e._set_speed = mock.AsyncMock()
        await e.handle_command({"cmd": "speed", "value": "fast"})
        e._set_speed.assert_not_called()

    async def test_valid_speed_is_forwarded(self):
        e = _engine()
        e._set_speed = mock.AsyncMock()
        await e.handle_command({"cmd": "speed", "value": 5})
        e._set_speed.assert_awaited_once_with(5)

    async def test_lap_telemetry_bad_arg_types_are_rejected(self):
        e = _engine()
        e._send_lap_telemetry = mock.AsyncMock()
        # lap must be an int, driver a str — a string lap would crash downstream
        await e.handle_command({"cmd": "getLapTelemetry", "driver": "44", "lap": "12"})
        e._send_lap_telemetry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
