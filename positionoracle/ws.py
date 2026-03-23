"""WebSocket manager for broadcasting updates to connected browsers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages active browser WebSocket connections.

    Handles connect/disconnect lifecycle and broadcasts portfolio updates
    to all connected clients.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        """Return the number of active connections."""
        return len(self._connections)

    @property
    def has_connections(self) -> bool:
        """Return True if at least one client is connected."""
        return bool(self._connections)

    async def connect(self, ws: WebSocket) -> None:
        """Accept and register a new WebSocket connection.

        Parameters
        ----------
        ws : WebSocket
            The incoming WebSocket connection.
        """
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        logger.info("Browser connected, total=%d", len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        """Remove a WebSocket connection.

        Parameters
        ----------
        ws : WebSocket
            The disconnecting WebSocket.
        """
        async with self._lock:
            self._connections.discard(ws)
        logger.info("Browser disconnected, total=%d", len(self._connections))

    async def broadcast(self, data: dict[str, Any]) -> None:
        """Send a JSON message to all connected clients.

        Silently removes any connections that have closed.

        Parameters
        ----------
        data : dict[str, Any]
            JSON-serializable data to broadcast.
        """
        message = json.dumps(data, default=str)
        dead: list[WebSocket] = []

        for ws in list(self._connections):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(message)
                else:
                    dead.append(ws)
            except (WebSocketDisconnect, RuntimeError):
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)
