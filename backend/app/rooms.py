"""
Generic websocket rooms: presence, broadcast, and a shared state blob.

This is the thin version on purpose. It knows nothing about any game -- a game
is a module that owns the `state` dict and validates transitions. Right now any
member may patch state, which is fine for a closed group of friends and is the
seam where a real game's rules will go.

Rooms live in process memory, so the API must run as a single instance (see
fly.toml). That is the right trade at this size; moving to multiple machines
means putting this behind Redis pub/sub.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

MAX_ROOM_MEMBERS = 32
MAX_STATE_BYTES = 64 * 1024

# Lobby chat (see routes/rooms.py _chat): a room keeps its last CHAT_HISTORY messages in memory, never on
# disk, for whoever joins next; each connection may send CHAT_RATE_COUNT per CHAT_RATE_WINDOW seconds.
CHAT_HISTORY = 50
CHAT_MAX_CHARS = 500
CHAT_RATE_COUNT = 8
CHAT_RATE_WINDOW = 10.0


@dataclass
class Member:
    ws: WebSocket
    user_id: str
    handle: str
    display_name: str
    avatar_url: str
    chat_times: deque = field(default_factory=lambda: deque(maxlen=CHAT_RATE_COUNT))

    def public(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "handle": self.handle,
            "displayName": self.display_name,
            "avatarUrl": self.avatar_url,
        }

    def may_chat(self, now: float) -> bool:
        """Sliding-window flood guard: at most CHAT_RATE_COUNT messages per CHAT_RATE_WINDOW seconds."""
        times = self.chat_times
        if len(times) == times.maxlen and now - times[0] < CHAT_RATE_WINDOW:
            return False
        times.append(now)
        return True


@dataclass
class Room:
    room_id: str
    members: dict[WebSocket, Member] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    chat: deque = field(default_factory=lambda: deque(maxlen=CHAT_HISTORY))

    def roster(self) -> list[dict[str, Any]]:
        # One entry per person, not per socket -- a laptop and a desktop are one player.
        seen: dict[str, dict[str, Any]] = {}
        for m in self.members.values():
            seen.setdefault(m.user_id, m.public())
        return list(seen.values())

    async def broadcast(self, message: dict[str, Any], skip: WebSocket | None = None) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.members):
            if ws is skip:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.members.pop(ws, None)


class RoomManager:
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        self._lock = asyncio.Lock()

    async def join(self, room_id: str, member: Member) -> Room:
        async with self._lock:
            room = self._rooms.setdefault(room_id, Room(room_id))
            if len(room.members) >= MAX_ROOM_MEMBERS:
                raise ValueError("room is full")
            room.members[member.ws] = member
        await room.broadcast(
            {"type": "join", "member": member.public(), "members": room.roster()},
            skip=member.ws,
        )
        return room

    async def leave(self, room_id: str, ws: WebSocket) -> None:
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                return
            member = room.members.pop(ws, None)
            empty = not room.members
            if empty:
                self._rooms.pop(room_id, None)
        if member is not None and not empty:
            await room.broadcast(
                {"type": "leave", "member": member.public(), "members": room.roster()}
            )

    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    def summary(self) -> list[dict[str, Any]]:
        return [
            {"roomId": r.room_id, "members": len(r.roster())}
            for r in self._rooms.values()
        ]


manager = RoomManager()
