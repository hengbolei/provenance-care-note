import asyncio
from collections import defaultdict
from uuid import UUID

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from carenote.domain.models import Actor


class PatientRealtimeHub:
    def __init__(self) -> None:
        self._connections: dict[UUID, dict[WebSocket, Actor]] = defaultdict(dict)

    async def connect(self, patient_id: UUID, socket: WebSocket, actor: Actor) -> None:
        await socket.accept()
        self._connections[patient_id][socket] = actor
        await self._broadcast_presence(patient_id)

    async def serve(self, patient_id: UUID, socket: WebSocket, revision_reader) -> None:
        revision = revision_reader()
        try:
            while True:
                await asyncio.sleep(1)
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
