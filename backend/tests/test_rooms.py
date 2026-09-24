import pytest
from starlette.websockets import WebSocketDisconnect

from app.auth import issue_ws_ticket
from tests.conftest import auth, make_user


def url(room: str, ticket: str) -> str:
    return f"/v1/rooms/{room}/ws?ticket={ticket}"


async def test_invalid_ticket_is_closed(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url("lobby", "garbage")) as ws:
            ws.receive_json()
    assert exc.value.code == 4401


async def test_join_broadcasts_presence_and_messages(client):
    a_id, a_tok = await make_user("ash", 30)
    b_id, _ = await make_user("gary", 31)

    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        hello = a.receive_json()
        assert hello["type"] == "welcome"
        assert hello["you"]["handle"] == "ash"
        assert [m["handle"] for m in hello["members"]] == ["ash"]

        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b:
            # A sees B arrive.
            joined = a.receive_json()
            assert joined["type"] == "join"
            assert joined["member"]["handle"] == "gary"
            assert {m["handle"] for m in joined["members"]} == {"ash", "gary"}

            b.receive_json()  # b's own welcome
            b.send_json({"type": "say", "data": {"hi": "there"}})

            heard = a.receive_json()
            assert heard["type"] == "say"
            assert heard["from"]["handle"] == "gary"
            assert heard["data"] == {"hi": "there"}

        left = a.receive_json()
        assert left["type"] == "leave"
        assert left["member"]["handle"] == "gary"


async def test_shared_state_merges_and_broadcasts(client):
    a_id, _ = await make_user("ash", 32)
    b_id, _ = await make_user("gary", 33)

    with client.websocket_connect(url("game", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        a.send_json({"type": "state", "patch": {"turn": "ash", "round": 1}})
        assert a.receive_json()["state"] == {"turn": "ash", "round": 1}

        with client.websocket_connect(url("game", issue_ws_ticket(b_id))) as b:
            # A late joiner is handed current state, not an empty room.
            welcome = b.receive_json()
            assert welcome["state"] == {"turn": "ash", "round": 1}
            a.receive_json()  # join notice

            b.send_json({"type": "state", "patch": {"turn": "gary"}})
            assert b.receive_json()["state"] == {"turn": "gary", "round": 1}


async def test_malformed_frames_are_reported_not_fatal(client):
    a_id, _ = await make_user("ash", 34)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        a.send_text("{not json")
        assert a.receive_json() == {"type": "error", "error": "malformed json"}

        a.send_json({"type": "state", "patch": "nope"})
        assert "object" in a.receive_json()["error"]

        a.send_json({"type": "ping"})
        assert a.receive_json() == {"type": "pong"}


async def test_empty_rooms_are_reaped(client):
    a_id, a_tok = await make_user("ash", 35)
    with client.websocket_connect(url("ephemeral", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        assert client.get("/v1/rooms", headers=auth(a_tok)).json()["rooms"] == [
            {"roomId": "ephemeral", "members": 1}
        ]
    assert client.get("/v1/rooms", headers=auth(a_tok)).json()["rooms"] == []


async def test_nudge_is_directed_and_carries_no_url(client):
    a_id, _ = await make_user("ash", 40)
    b_id, _ = await make_user("gary", 41)
    c_id, _ = await make_user("misty", 42)

    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()  # welcome
        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b:
            a.receive_json()  # join notice for b
            b.receive_json()  # b welcome
            with client.websocket_connect(url("lobby", issue_ws_ticket(c_id))) as c:
                a.receive_json(); b.receive_json()  # join notices for c
                c.receive_json()  # c welcome

                # Ash nudges Gary, with an unsafe note that should be trimmed.
                a.send_json({"type": "nudge", "to": b_id, "note": "  come look\x07  " + "x" * 200})

                got = b.receive_json()
                assert got["type"] == "nudge"
                assert got["from"]["handle"] == "ash"
                assert "url" not in got and "link" not in got  # never a URL
                assert got["note"].startswith("come look")
                assert len(got["note"]) <= 120

                ack = a.receive_json()
                assert ack == {"type": "nudge_ack", "to": b_id, "delivered": 1}

                # Misty (not the target) receives nothing on her socket.
                c.send_json({"type": "ping"})
                assert c.receive_json() == {"type": "pong"}


async def test_nudge_requires_a_target(client):
    a_id, _ = await make_user("ash", 43)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        a.send_json({"type": "nudge"})
        assert "target" in a.receive_json()["error"]


async def test_signal_is_directed_and_never_broadcast(client):
    # WebRTC setup (SDP / ICE, which carry IP addresses) must reach only the member it's for.
    a_id, _ = await make_user("ash", 50)
    b_id, _ = await make_user("gary", 51)
    c_id, _ = await make_user("misty", 52)

    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()  # welcome
        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b:
            a.receive_json()  # join notice for b
            b.receive_json()  # b welcome
            with client.websocket_connect(url("lobby", issue_ws_ticket(c_id))) as c:
                a.receive_json(); b.receive_json()  # join notices for c
                c.receive_json()  # c welcome

                offer = {"kind": "offer", "toPeer": "b1", "fromPeer": "a1", "sdp": "v=0 ..."}
                a.send_json({"type": "signal", "to": b_id, "data": offer})

                got = b.receive_json()
                assert got == {"type": "signal", "from": got["from"], "data": offer}
                assert got["from"]["handle"] == "ash"

                # Neither the sender nor a bystander hears it.
                a.send_json({"type": "ping"})
                assert a.receive_json() == {"type": "pong"}
                c.send_json({"type": "ping"})
                assert c.receive_json() == {"type": "pong"}


async def test_signal_reaches_every_socket_of_the_target(client):
    a_id, _ = await make_user("ash", 53)
    b_id, _ = await make_user("gary", 54)

    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b1:
            a.receive_json(); b1.receive_json()
            with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b2:
                a.receive_json(); b1.receive_json(); b2.receive_json()  # join notices + welcome

                a.send_json({"type": "signal", "to": b_id, "data": {"kind": "hello"}})
                assert b1.receive_json()["data"] == {"kind": "hello"}
                assert b2.receive_json()["data"] == {"kind": "hello"}


async def test_signal_needs_a_target_and_an_object(client):
    a_id, _ = await make_user("ash", 55)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        a.send_json({"type": "signal", "data": {"kind": "offer"}})
        assert "target" in a.receive_json()["error"]
        a.send_json({"type": "signal", "to": a_id, "data": "not an object"})
        assert "object" in a.receive_json()["error"]


def chat(ws, text):
    ws.send_json({"type": "say", "data": {"kind": "chat", "text": text}})


async def test_chat_is_cleaned_clipped_and_stamped(client):
    a_id, _ = await make_user("ash", 60)
    b_id, _ = await make_user("gary", 61)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b:
            a.receive_json(); b.receive_json()  # join notice + welcome

            chat(a, "  hi\x07 there\n\n  friend  ")
            got = b.receive_json()
            assert got["type"] == "say" and got["from"]["handle"] == "ash"
            assert got["data"] == {"kind": "chat", "text": "hi there friend"}  # control chars gone, spaces folded
            assert got["at"].endswith("+00:00") and len(got["id"]) == 12
            assert a.receive_json()["id"] == got["id"]  # the sender gets the same echo

            chat(a, "x" * 800)
            assert len(b.receive_json()["data"]["text"]) == 500

            # Other room traffic on "say" is still relayed untouched.
            a.send_json({"type": "say", "data": {"kind": "game", "move": 3}})
            assert b.receive_json()["data"] == {"kind": "game", "move": 3}


async def test_joiners_get_the_last_50_chat_messages(client, monkeypatch):
    monkeypatch.setattr("app.rooms.CHAT_RATE_WINDOW", 0.0)  # 55 messages in a row: no flood guard for this one
    a_id, _ = await make_user("ash", 62)
    b_id, _ = await make_user("gary", 63)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        for i in range(55):
            chat(a, f"message {i}")
            a.receive_json()  # own echo
        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b:
            history = b.receive_json()["chat"]
            assert [m["data"]["text"] for m in history] == [f"message {i}" for i in range(5, 55)]
            assert all(m["from"]["handle"] == "ash" and m["at"] and m["id"] for m in history)


async def test_chat_is_rate_limited_per_connection(client):
    a_id, _ = await make_user("ash", 64)
    b_id, _ = await make_user("gary", 65)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        with client.websocket_connect(url("lobby", issue_ws_ticket(b_id))) as b:
            a.receive_json(); b.receive_json()
            for i in range(9):
                chat(a, f"spam {i}")
            echoes = [a.receive_json() for _ in range(9)]
            assert [e["data"]["text"] for e in echoes[:8]] == [f"spam {i}" for i in range(8)]
            assert echoes[8]["type"] == "error" and echoes[8]["error"].startswith("chat: slow down")
            got = [b.receive_json()["data"]["text"] for _ in range(8)]
            assert got == [f"spam {i}" for i in range(8)]
            b.send_json({"type": "ping"})
            assert b.receive_json() == {"type": "pong"}  # the ninth never reached gary


async def test_empty_or_non_text_chat_is_refused(client):
    a_id, _ = await make_user("ash", 66)
    with client.websocket_connect(url("lobby", issue_ws_ticket(a_id))) as a:
        a.receive_json()
        chat(a, "   \x07  ")
        assert a.receive_json()["error"] == "chat: empty message"
        a.send_json({"type": "say", "data": {"kind": "chat", "text": 42}})
        assert a.receive_json()["error"] == "chat: text must be a string"
