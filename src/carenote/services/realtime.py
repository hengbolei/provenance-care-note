import asyncio
import itertools
from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from carenote.domain.models import Actor, Role

EVENT_BACKLOG = 200


class PatientRealtimeHub:
    def __init__(self) -> None:
        self._connections: dict[UUID, dict[WebSocket, Actor]] = defaultdict(dict)
        self._events: dict[UUID, deque[tuple[int, dict[str, Any]]]] = defaultdict(
            lambda: deque(maxlen=EVENT_BACKLOG)
        )
        self._sequence = itertools.count(1)

    def publish(self, patient_id: UUID, event: dict[str, Any]) -> None:
        """Queue an event for this patient's sockets; safe to call from worker threads.

        Events marked `"audience": "care_team"` are never sent to a patient's own socket.
        """
        self._events[patient_id].append((next(self._sequence), event))

    def events_since(
        self, patient_id: UUID, after: int, actor: Actor
    ) -> tuple[int, list[dict[str, Any]]]:
        latest = after
        visible: list[dict[str, Any]] = []
        for sequence, event in list(self._events.get(patient_id, ())):
            if sequence <= after:
                continue
            latest = sequence
            if event.get("audience") == "care_team" and actor.role is Role.PATIENT:
                continue
            visible.append(event)
        return latest, visible

    async def connect(self, patient_id: UUID, socket: WebSocket, actor: Actor) -> None:
        await socket.accept()
        self._connections[patient_id][socket] = actor
        await self._broadcast_presence(patient_id)

    async def serve(self, patient_id: UUID, socket: WebSocket, revision_reader) -> None:
        revision = revision_reader()
        actor = self._connections[patient_id][socket]
        seen, _ = self.events_since(patient_id, 0, actor)
        try:
            while True:
                await asyncio.sleep(1)
                seen, events = self.events_since(patient_id, seen, actor)
                for event in events:
                    await socket.send_json(event)
                current = revision_reader()
                if current != revision:
                    revision = current
                    await socket.send_json({"type": "refresh", "revision": revision})
                else:
                    await socket.send_json({"type": "heartbeat"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self._connections[patient_id].pop(socket, None)
            if not self._connections[patient_id]:
                self._connections.pop(patient_id, None)
            else:
                await self._broadcast_presence(patient_id)

    async def _broadcast_presence(self, patient_id: UUID) -> None:
        connections = self._connections.get(patient_id, {})
        roles = sorted({actor.role.value for actor in connections.values()})
        payload = {"type": "presence", "count": len(connections), "roles": roles}
        stale: list[WebSocket] = []
        for socket in connections:
            try:
                await socket.send_json(payload)
            except RuntimeError:
                stale.append(socket)
        for socket in stale:
            connections.pop(socket, None)


realtime_hub = PatientRealtimeHub()
