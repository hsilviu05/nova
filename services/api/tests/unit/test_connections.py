"""The device connection registry."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nova.services.connections import InMemoryConnectionRegistry


class RecordingTransport:
    """A transport double that records what was sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


class BrokenTransport:
    """A transport whose peer has gone away."""

    async def send_json(self, data: dict[str, Any]) -> None:
        raise ConnectionResetError("peer is gone")


@pytest.fixture
def registry() -> InMemoryConnectionRegistry:
    return InMemoryConnectionRegistry()


class TestDelivery:
    async def test_sends_to_a_registered_device(self, registry: InMemoryConnectionRegistry) -> None:
        device_id = uuid.uuid4()
        transport = RecordingTransport()
        await registry.register(device_id, transport)

        assert await registry.send(device_id, {"type": "ping"}) is True
        assert transport.sent == [{"type": "ping"}]

    async def test_reports_failure_for_an_unknown_device(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        assert await registry.send(uuid.uuid4(), {"type": "ping"}) is False

    async def test_does_not_deliver_to_another_device(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        first, second = RecordingTransport(), RecordingTransport()
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        await registry.register(first_id, first)
        await registry.register(second_id, second)

        await registry.send(first_id, {"for": "first"})

        assert first.sent == [{"for": "first"}]
        assert second.sent == []

    async def test_a_failing_send_unregisters_the_device(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        """A send failure means the peer is gone, whatever the map says."""
        device_id = uuid.uuid4()
        await registry.register(device_id, BrokenTransport())

        assert await registry.send(device_id, {"type": "ping"}) is False
        assert await registry.is_connected(device_id) is False


class TestLifecycle:
    async def test_tracks_connection_state(self, registry: InMemoryConnectionRegistry) -> None:
        device_id = uuid.uuid4()
        assert await registry.is_connected(device_id) is False

        await registry.register(device_id, RecordingTransport())
        assert await registry.is_connected(device_id) is True

        await registry.unregister(device_id)
        assert await registry.is_connected(device_id) is False

    async def test_unregistering_an_unknown_device_is_harmless(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        await registry.unregister(uuid.uuid4())

    async def test_reconnect_displaces_the_previous_connection(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        """The newest connection is by definition the reachable one."""
        device_id = uuid.uuid4()
        old, new = RecordingTransport(), RecordingTransport()

        await registry.register(device_id, old)
        await registry.register(device_id, new)
        await registry.send(device_id, {"type": "ping"})

        assert new.sent == [{"type": "ping"}]
        assert old.sent == []

    async def test_a_stale_cleanup_does_not_evict_the_replacement(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        """The race this method exists for.

        A dropped socket's cleanup can run *after* the device has already
        reconnected. An unconditional unregister would then kill the live
        connection, leaving the device unreachable until it reconnected again.
        """
        device_id = uuid.uuid4()
        old, new = RecordingTransport(), RecordingTransport()

        await registry.register(device_id, old)
        await registry.register(device_id, new)

        # The old connection's cleanup arrives late.
        await registry.unregister_if_current(device_id, old)

        assert await registry.is_connected(device_id) is True
        assert await registry.send(device_id, {"type": "ping"}) is True
        assert new.sent == [{"type": "ping"}]

    async def test_current_cleanup_does_unregister(
        self, registry: InMemoryConnectionRegistry
    ) -> None:
        device_id = uuid.uuid4()
        transport = RecordingTransport()
        await registry.register(device_id, transport)

        await registry.unregister_if_current(device_id, transport)

        assert await registry.is_connected(device_id) is False

    async def test_counts_connections(self, registry: InMemoryConnectionRegistry) -> None:
        for _ in range(3):
            await registry.register(uuid.uuid4(), RecordingTransport())
        assert await registry.connected_count() == 3
