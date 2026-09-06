"""Registry of live device connections.

Behind a protocol so the in-process implementation can be swapped for a
Redis-backed one without touching callers. That swap is what horizontal
scaling needs: with connections held in process memory, a command issued on
one API instance cannot reach a device connected to another. Single-instance
today, noted as a Phase 10 concern in ADR 004.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from nova.core.logging import get_logger

logger = get_logger(__name__)


class DeviceTransport(Protocol):
    """The part of a WebSocket the registry needs.

    Narrow on purpose: tests substitute a recording double without standing
    up a real socket.
    """

    async def send_json(self, data: dict[str, object]) -> None: ...


class ConnectionRegistry(Protocol):
    """Tracks which devices are currently connected."""

    async def register(self, device_id: uuid.UUID, transport: DeviceTransport) -> None: ...

    async def unregister(self, device_id: uuid.UUID) -> None: ...

    async def unregister_if_current(
        self, device_id: uuid.UUID, transport: DeviceTransport
    ) -> None: ...

    async def send(self, device_id: uuid.UUID, message: dict[str, object]) -> bool: ...

    async def is_connected(self, device_id: uuid.UUID) -> bool: ...


class InMemoryConnectionRegistry:
    """Holds connections in this process.

    A lock guards the map because a device that reconnects before its old
    socket is cleaned up would otherwise race between register and
    unregister, and the loser would evict the live connection.
    """

    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, DeviceTransport] = {}
        self._lock = asyncio.Lock()

    async def register(self, device_id: uuid.UUID, transport: DeviceTransport) -> None:
        """Attach ``transport``, displacing any previous connection.

        A device that reconnects after a network drop may have a stale socket
        still registered. Last writer wins, since the newest connection is by
        definition the reachable one.
        """
        async with self._lock:
            existing = self._connections.get(device_id)
            self._connections[device_id] = transport

        if existing is not None:
            logger.info("device_connection_replaced", device_id=str(device_id))

    async def unregister(self, device_id: uuid.UUID) -> None:
        """Detach a device, if it is still the registered one."""
        async with self._lock:
            self._connections.pop(device_id, None)

    async def unregister_if_current(self, device_id: uuid.UUID, transport: DeviceTransport) -> None:
        """Detach only if ``transport`` is still the registered connection.

        A closing socket must not evict the replacement that displaced it,
        which is what an unconditional ``unregister`` would do when an old
        connection's cleanup runs after a reconnect.
        """
        async with self._lock:
            if self._connections.get(device_id) is transport:
                del self._connections[device_id]

    async def send(self, device_id: uuid.UUID, message: dict[str, object]) -> bool:
        """Deliver a message. False when the device is not connected."""
        async with self._lock:
            transport = self._connections.get(device_id)

        if transport is None:
            return False

        try:
            await transport.send_json(message)
        except Exception as exc:
            logger.warning("device_send_failed", device_id=str(device_id), error=str(exc))
            await self.unregister_if_current(device_id, transport)
            return False
        return True

    async def is_connected(self, device_id: uuid.UUID) -> bool:
        async with self._lock:
            return device_id in self._connections

    async def connected_count(self) -> int:
        async with self._lock:
            return len(self._connections)
