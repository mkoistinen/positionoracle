from unittest.mock import AsyncMock

from starlette.websockets import WebSocketState

from positionoracle.ws import ConnectionManager


class TestConnectionManager:
    async def test_initial_state(self):
        mgr = ConnectionManager()
        assert mgr.active_count == 0
        assert not mgr.has_connections

    async def test_connect_and_disconnect(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        assert mgr.active_count == 1
        assert mgr.has_connections
        ws.accept.assert_awaited_once()

        await mgr.disconnect(ws)
        assert mgr.active_count == 0
        assert not mgr.has_connections

    async def test_multiple_connections(self):
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await mgr.connect(ws1)
        await mgr.connect(ws2)
        assert mgr.active_count == 2

        await mgr.disconnect(ws1)
        assert mgr.active_count == 1

    async def test_broadcast(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        ws.client_state = WebSocketState.CONNECTED
        await mgr.connect(ws)

        await mgr.broadcast({"type": "test", "value": 42})
        ws.send_text.assert_awaited_once()
        sent = ws.send_text.call_args[0][0]
        assert '"type": "test"' in sent

    async def test_broadcast_removes_dead_connections(self):
        mgr = ConnectionManager()
        ws_alive = AsyncMock()
        ws_alive.client_state = WebSocketState.CONNECTED
        ws_dead = AsyncMock()
        ws_dead.client_state = WebSocketState.DISCONNECTED

        await mgr.connect(ws_alive)
        await mgr.connect(ws_dead)
        assert mgr.active_count == 2

        await mgr.broadcast({"type": "test"})
        assert mgr.active_count == 1

    async def test_broadcast_handles_send_error(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        ws.client_state = WebSocketState.CONNECTED
        ws.send_text.side_effect = RuntimeError("closed")
        await mgr.connect(ws)

        await mgr.broadcast({"type": "test"})
        assert mgr.active_count == 0

    async def test_disconnect_idempotent(self):
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        await mgr.disconnect(ws)
        await mgr.disconnect(ws)
        assert mgr.active_count == 0

    async def test_broadcast_empty(self):
        mgr = ConnectionManager()
        await mgr.broadcast({"type": "test"})
