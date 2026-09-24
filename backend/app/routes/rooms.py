"""Websocket rooms: presence, broadcast, shared state, lobby chat."""
import json
import secrets
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from .. import rooms
from ..auth import Caller, read_ws_ticket, require_device
from ..db import SessionLocal, get_session
from ..models import User
from ..rooms import MAX_STATE_BYTES, Member, Room, manager

router = APIRouter(prefix="/v1/rooms", tags=["rooms"])

MAX_FRAME_BYTES = 16 * 1024


@router.get("")
async def list_rooms(caller: Caller = Depends(require_device)) -> dict:
    return {"rooms": manager.summary()}


@router.websocket("/{room_id}/ws")
async def room_ws(
    websocket: WebSocket, room_id: str, ticket: str = Query(...)
) -> None:
    user_id = read_ws_ticket(ticket)
    if user_id is None:
        await websocket.close(code=4401, reason="invalid or expired ticket")
        return

    room_id = room_id.strip()[:64]
    if not room_id:
        await websocket.close(code=4400, reason="bad room id")
        return

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
    if user is None or not user.is_active:
        await websocket.close(code=4403, reason="account disabled")
        return

    await websocket.accept()
    member = Member(
        ws=websocket,
        user_id=user.id,
        handle=user.handle,
        display_name=user.display_name or user.handle,
        avatar_url=user.avatar_url,
    )

    try:
        room = await manager.join(room_id, member)
    except ValueError as exc:
        await websocket.close(code=4429, reason=str(exc))
        return

    await websocket.send_json({
        "type": "welcome",
        "room": room_id,
        "you": member.public(),
        "members": room.roster(),
        "state": room.state,
        "chat": list(room.chat),   # recent lobby chat, oldest first, so a joiner can catch up
    })

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > MAX_FRAME_BYTES:
                await websocket.send_json({"type": "error", "error": "frame too large"})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "malformed json"})
                continue
            await _handle(room_id, member, msg)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.leave(room_id, websocket)


async def _handle(room_id: str, member: Member, msg: dict) -> None:
    room = manager.get(room_id)
    if room is None:
        return
    kind = msg.get("type")

    if kind == "say":
        data = msg.get("data")
        if isinstance(data, dict) and data.get("kind") == "chat":
            await _chat(room, member, data)
            return
        await room.broadcast({"type": "say", "from": member.public(), "data": data})
        return

    if kind == "state":
        # Any member may patch shared state. That is intentional for a trusted
        # group; a real game replaces this branch with validated transitions.
        patch = msg.get("patch")
        if not isinstance(patch, dict):
            await member.ws.send_json({"type": "error", "error": "patch must be an object"})
            return
        merged = {**room.state, **patch}
        if len(json.dumps(merged)) > MAX_STATE_BYTES:
            await member.ws.send_json({"type": "error", "error": "state too large"})
            return
        room.state = merged
        await room.broadcast({"type": "state", "state": room.state, "by": member.public()})
        return

    if kind == "nudge":
        # A directed "hey, come look at the Arena" ping. Deliberately carries no
        # URL or command -- only who it's from and an optional short note -- so a
        # nudge can never make the recipient's machine open or do anything. The
        # recipient's client just shows a toast; acting on it is their choice.
        target = msg.get("to")
        if not isinstance(target, str) or not target:
            await member.ws.send_json({"type": "error", "error": "nudge needs a target userId"})
            return
        note = msg.get("note")
        note = ("".join(ch for ch in note if ch.isprintable()).strip()[:120]
                if isinstance(note, str) else "")
        payload = {"type": "nudge", "from": member.public(), "note": note}
        delivered = 0
        for ws, m in list(room.members.items()):
            if m.user_id == target and ws is not member.ws:
                try:
                    await ws.send_json(payload)
                    delivered += 1
                except Exception:
                    pass
        await member.ws.send_json({"type": "nudge_ack", "to": target, "delivered": delivered})
        return

    if kind == "signal":
        # WebRTC setup for lobby voice: an offer / answer / ICE candidate for ONE
        # member, relayed only to that member's sockets and never broadcast -- SDP and
        # ICE candidates carry IP addresses, so only the people you're actually
        # talking to see yours. The payload is opaque here; MAX_FRAME_BYTES bounds it.
        # The audio itself never touches this server: it flows browser to browser.
        target = msg.get("to")
        data = msg.get("data")
        if not isinstance(target, str) or not target:
            await member.ws.send_json({"type": "error", "error": "signal needs a target userId"})
            return
        if not isinstance(data, dict):
            await member.ws.send_json({"type": "error", "error": "signal data must be an object"})
            return
        payload = {"type": "signal", "from": member.public(), "data": data}
        for ws, m in list(room.members.items()):
            if m.user_id == target and ws is not member.ws:
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass
        return

    if kind == "ping":
        await member.ws.send_json({"type": "pong"})
        return

    await member.ws.send_json({"type": "error", "error": f"unknown message type: {kind!r}"})


async def _chat(room: Room, member: Member, data: dict) -> None:
    """Lobby chat. It rides the generic "say" relay (as {kind: "chat", text}), so pages that predate this
    server keep chatting with pages that don't. Here the server cleans and clips the text, rate-limits each
    connection, stamps the time and an id, and keeps the last CHAT_HISTORY messages in memory -- never on
    disk -- for whoever joins next; they go when the room empties or the server restarts."""
    text = data.get("text")
    if not isinstance(text, str):
        await member.ws.send_json({"type": "error", "error": "chat: text must be a string"})
        return
    # Control characters (bells, newlines, escapes) become spaces; runs of whitespace fold to one.
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:rooms.CHAT_MAX_CHARS]
    if not text:
        await member.ws.send_json({"type": "error", "error": "chat: empty message"})
        return
    if not member.may_chat(time.monotonic()):
        await member.ws.send_json({
            "type": "error",
            "error": "chat: slow down — at most %d messages every %d seconds"
                     % (rooms.CHAT_RATE_COUNT, rooms.CHAT_RATE_WINDOW),
        })
        return
    entry = {
        "type": "say", "from": member.public(), "data": {"kind": "chat", "text": text},
        "id": secrets.token_hex(6), "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    room.chat.append(entry)
    await room.broadcast(entry)
