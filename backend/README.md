# Claude HQ Arena

The multiplayer backend for [Claude HQ](../README.md): a shared leaderboard,
seasons, and websocket rooms for minigames.

## What crosses the network

Claude HQ reads your transcripts. This backend never sees them.

The local client (`arena.py`) sends **daily counts only** — prompts, tool calls,
artifacts, tokens. It never sends prompt text, replies, file paths, project or
folder names, session ids, or titles. Two details worth knowing:

- **Tool names are allowlisted.** MCP tools are named `mcp__<server>__<tool>` and
  routinely carry an employer's or client's name, so anything that isn't a
  built-in Claude Code tool is bucketed as `Other` before it can reach the wire.
- **Cost is opt-in and off by default.** Spend is salary- and employer-adjacent.

`StatPayload` uses `extra="forbid"`, so a client that grows a new field cannot
silently start leaking it — the server rejects the whole submission until that
field is added to `app/schemas.py` deliberately.

## Scoring is the server's job

Clients submit raw activity, never a score. XP, levels, ranks and streaks are
derived in `app/scoring.py`. That means the formula can change without a client
release, and faking a score means faking plausible daily activity rather than
POSTing `{"xp": 999999}`.

There is no anti-cheat beyond sanity clamps (`ARENA_MAX_DAILY_*`), and that's
deliberate: the client runs on your friends' machines. The clamps exist so a
client bug or a prank can't permanently distort the board, not to stop a
determined faker.

## Layout

| Path | What |
|---|---|
| `app/schemas.py` | The wire allowlist. The privacy boundary. |
| `app/scoring.py` | XP / level / rank / streak. Mirrors `dashboard.py`. |
| `app/service.py` | Ingest (upsert per day) and leaderboard queries. |
| `app/rooms.py` | In-process websocket rooms: presence, broadcast, shared state. |
| `app/auth.py` | Device tokens, pairing codes, websocket tickets. |
| `alembic/` | Migrations. |

`daily_stats` is **upserted** per (user, day), never summed — the client rescans
whole transcripts, so each submission is authoritative for that day.
`stat_snapshots` keeps every raw submission so the board can be recomputed if
the scoring rules change.

## Running locally

```bash
cd backend
uv sync
cp .env.example .env          # defaults to SQLite; no Postgres needed
uv run uvicorn app.main:app --reload --port 8080
uv run pytest                 # 22 tests
```

### Seeding fake friends

A leaderboard with one row tells you nothing, so there's a seeder. It creates
users and device tokens directly, then publishes their stats **over HTTP through
the real API**, exercising auth and the schema allowlist rather than just the ORM:

```bash
uv run python scripts/seed_demo.py --reset --friends 6
```

It prints the resulting board and each device token, so you can curl as any of
them:

```bash
curl -s localhost:8080/v1/board?window=30d -H "Authorization: Bearer hqd_..." | jq
```

Point it at a local server only — it writes users it invented.

## Deploying

Two supported shapes, both driven by a wizard from the repo root:

| | `./selfhost-wizard.sh` | `./deploy-wizard.sh` |
|---|---|---|
| Runs on | your own Mac | Fly.io |
| Database | SQLite (one file) | Neon Postgres |
| Reachable via | Cloudflare Tunnel | `*.fly.dev` |
| Cost | free | ~$5/mo |
| Up when | your Mac is awake | always |

Both stop and tell you when a step needs a browser.

### Self-hosting notes

SQLite is the primary database when self-hosting, so `app/db.py` sets WAL
(readers don't block while a publish writes), a 5s busy timeout, and foreign
keys. Back it up by copying `backend/arena.db`.

Two things are anchored to absolute paths on purpose, because launchd starts
the service from a different working directory and relative paths would fail
*silently* rather than loudly:

- `ARENA_DATABASE_URL` uses the four-slash form (`sqlite+aiosqlite:////abs/path`)
  — a relative path would create a second, empty database elsewhere.
- `env_file` in `app/config.py` is resolved against the package directory —
  otherwise the service would load no config at all and quietly fall back to
  defaults with no OAuth configured.

Your Mac sleeping is not destructive: publishes retry every 5 minutes and each
one carries a 30-day window, so nothing is lost. The board is simply
unreachable until the machine is back.

## Scale note

Rooms hold presence and shared state **in process memory**, so this service must
run as exactly one machine (`fly.toml` sets `min_machines_running = 1` and
`auto_stop_machines = false`). For a group of friends that is the right trade.
Scaling out means putting rooms behind Redis pub/sub first — `app/rooms.py` is
the only file that changes.

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | |
| `GET` | `/v1/auth/github/start` | Redirects to GitHub |
| `GET` | `/v1/auth/github/callback` | Shows a one-shot pairing code |
| `POST` | `/v1/auth/pair` | Code → device token |
| `POST` | `/v1/auth/ticket` | Device token → 60s websocket ticket |
| `POST` | `/v1/stats` | Ingest. Bearer device token. |
| `GET` | `/v1/board?window=season\|30d\|7d\|all` | |
| `GET` | `/v1/board/stream` | SSE, pushes on ingest |
| `GET` | `/v1/me` | |
| `GET` | `/v1/rooms` | Open rooms |
| `WS` | `/v1/rooms/{room}/ws?ticket=` | Presence, `say` (a `{kind: "chat"}` payload is lobby chat: cleaned, rate-limited, last 50 kept in memory and sent in `welcome`), `state`, `nudge`, `signal` (WebRTC setup, to one member), `ping` |
