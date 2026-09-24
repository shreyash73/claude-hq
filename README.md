# ⚡ Claude HQ

**Version 1.1.1** · a **local, private, gamified dashboard** for everything happening across your Claude Code sessions.

Claude HQ reads your live sessions (`claude agents --json`) and your session transcripts
(`~/.claude/projects/**/*.jsonl`) and turns them into a single command center: what every tab is
working on right now, what it's cost you, a searchable archive of every past session, and a whole
Pokémon-style collection layer on top for fun.

It runs entirely on your machine and binds to `127.0.0.1` only. **Your conversations never leave your
computer.** (The one exception is that the "pokemon" creature pack loads sprite images from a public
CDN — only a Pokédex *number* is ever sent, never any of your data. Switch to the "monsters" pack for
100% offline. The optional [Arena](#-arena-multiplayer--optional) layer, off unless you turn it on,
shares daily activity *counts* with friends — never conversation content.)

---

## ✨ Features

**Fleet overview**
- Live cards for every session, grouped **Needs you → Working → Idle → Stale**, updated in real time
  over Server-Sent Events (with a polling fallback).
- **Archived history**: every past transcript shows up as a Stale card — your full history, searchable
  and re-openable.
- Per-card: current in-flight tool, first/latest prompt, last reply, tokens & cost, a 24h sparkline,
  tags, notes, and quick actions.
- **Actions**: ▶ Resume (opens a new terminal into `claude --resume <id>`), 📂 Reveal in Finder,
  ⤢ Focus mode, ✕ Close (stops the process), ✏️ Rename, 📌 Pin.

**Search & detail**
- Full-history **search** across every transcript (TF-IDF ranked).
- Detail **drawer** with a live-tailing timeline, files touched, token/cost, session history
  (started / active days / span), notes & tags, and a **full transcript reader** with in-transcript
  search + Markdown export.

**Analytics**
- 13-week **contribution heatmap** (click any day → that day's digest), token/cost trends,
  busiest-hours & day-of-week charts, **Hall of Fame**, and an **Account & Usage** panel
  (cost by model, cost by project).
- **Project deep-dive**: click any project for its own dashboard.
- **Daily Digest**: a generated "what I did across all sessions today" report (Markdown export).

**Gamification**
- Every session maps (stably) to one of **48 creatures** that **evolve** through real evolution lines
  as the session grows, with elemental **types** and rare **shiny** variants.
- A **Pokédex** collection view, a **⚔️ Gym** team-type-matchup analyzer (real 18-type chart),
  a **🏅 Quests** view, a **Trainer Card**, XP/levels, streaks, achievements, and confetti.

**Quality-of-life**
- Command palette (⌘/Ctrl-K), keyboard shortcuts (`?` for help, `1`–`5` for views, `/` search, `r`
  refresh), desktop notifications + optional chime + voice alerts when a tab needs you, a
  **War Room** rotating big-screen view, a **Focus Pomodoro** timer, and a **"welcome back" recap**.
- Settings: **themes** (Aurora / Midnight / Forest / Mono + High-contrast), **creature packs**,
  **accent color**, **large-text** and **calm** (reduced-motion) modes, refresh cadence, stuck-tab
  threshold, and a daily cost budget.
- Accessibility: skip link, ARIA roles/live regions, roving-tabindex grid navigation, focus trapping.

---

## 📦 Requirements

- **macOS** (uses `launchctl` for auto-start, `open`/AppleScript/`kitty` for actions).
- **Python 3** (standard library only — no `pip install` needed).
- **Claude Code** installed and on your `PATH` (`claude`).
- Optional: [`kitty`](https://sw.kovidgoyal.net/kitty/) terminal (Resume opens a kitty window; falls
  back to Terminal.app).

No third-party Python or JS dependencies. Two files do everything: `dashboard.py` + `index.html`
(plus `arena.py`, also stdlib-only, if you turn on Arena).

---

## 🚀 Installation

```bash
git clone <your-repo-url> claude-hq
cd claude-hq
python3 dashboard.py
```

That starts the server on <http://127.0.0.1:8765> and opens it in your browser.

### Flags
```
python3 dashboard.py [--port 8765] [--no-open]
```

### Run it always-on (auto-start at login)
```bash
python3 dashboard.py --install     # registers a launchd LaunchAgent (starts at login, self-heals)
python3 dashboard.py --uninstall   # removes it
python3 dashboard.py --print-plist # preview the LaunchAgent, no side effects
```
Once installed you never start it by hand again — just open <http://127.0.0.1:8765>.

> **Note:** the LaunchAgent runs the code as it is on disk. Frontend (`index.html`) changes are picked
> up on refresh; after editing `dashboard.py`, reload the backend with:
> ```bash
> launchctl kickstart -k gui/$(id -u)/com.claudehq.dashboard
> ```

### Optional shell alias
```bash
echo 'alias claude-hq="python3 ~/Documents/Claude/claude-dashboard/dashboard.py"' >> ~/.zshrc
```

---

## 🔒 Privacy & security

- Binds to **`127.0.0.1` only**; any request with a non-localhost `Host` header is rejected with `403`.
- All state-changing actions (rename / pin / close / resume / settings) require a per-process **CSRF
  token** (injected into the page, sent as `X-HQ-Token`) plus an Origin / `Sec-Fetch-Site` check.
- File lookups are validated (UUID / known-folder allowlists) — no path traversal.
- Your transcripts and settings stay on disk. `config.json` and `sessions-meta.json` are git-ignored.
- **Arena is off by default.** When enabled it publishes daily *counts* only — never conversation
  content, file paths or project names — and its device token lives in `arena-link.json`, outside
  `config.json`, so it is never served to the page. See [Arena](#-arena-multiplayer--optional).

---

## 🗂️ Files

| File | What |
|---|---|
| `dashboard.py` | Stdlib-only HTTP server: reads live agents + transcripts, serves the JSON API and the page. |
| `index.html` | The entire self-contained frontend (inline CSS + JS). |
| `arena.py` | Optional Arena client: builds + publishes the shared-stats payload. |
| `backend/` | Optional Arena server (FastAPI). Only needed by whoever hosts it. |
| `arena-link.json` | Your Arena device token. *(git-ignored)* |
| `config.json` | Your settings (theme, pack, budget, …). Created on first save. *(git-ignored)* |
| `sessions-meta.json` | Per-session pins / tags / notes / rename aliases. *(git-ignored)* |

### HTTP API (all `127.0.0.1` only)
`GET /` · `GET /api/sessions` · `GET /api/stream` (SSE) · `GET /api/session/<id>` ·
`GET /api/session/<id>/export.md` · `GET /api/transcript/<id>?offset&limit&q` · `GET /api/search?q=` ·
`GET /api/history` · `GET /api/project?folder=` · `GET /api/pokedex` · `GET /api/digest?date&download` ·
`GET /api/config` · `GET /api/meta` · `GET /api/export.{json,csv}` ·
`POST /api/action` · `POST /api/config` · `POST /api/meta` (all CSRF-guarded).

---

## 🏆 Arena (multiplayer) — optional

Claude HQ is local-first and stays that way. **Arena** is an opt-in layer that
adds a shared leaderboard across you and your friends, a lobby chat and a voice
channel with everyone who has Arena open, plus websocket rooms to build
minigames on. It is off until you connect it.

### What is shared

Your dashboard keeps reading transcripts locally and publishes **daily counts
only** — prompts, tool calls, artifacts, tokens. It never sends prompt text,
replies, file paths, project or folder names, session ids, or titles.

- **Tool names are allowlisted.** MCP tools are named `mcp__<server>__<tool>`
  and routinely carry an employer's or client's name, so anything that isn't a
  built-in Claude Code tool is bucketed as `Other` before it reaches the wire.
- **Cost sharing is off by default.** Spend is salary- and employer-adjacent.
- **Chat is only what you type.** Messages in the lobby chat go to everyone in
  the lobby, relayed by the server. It keeps the last 50 in memory (never on
  disk) so people who join can catch up; they're gone when the lobby empties or
  the server restarts. The server also cleans and caps messages (500
  characters) and rate-limits them (8 per 10 seconds per connection).
- **Voice is peer-to-peer.** Audio goes straight between browsers (WebRTC),
  never through the server. The server relays only the connection setup, which
  includes IP addresses, and only to the people you're in voice with; a public
  STUN server (Google's) tells your browser its public address. Your microphone
  is used only after you click **Join voice**, and it stops when you leave.
- The wire format rejects unknown fields outright, so a future client change
  can't silently start leaking one.

Scores are computed on the server from raw counts, not submitted by the client,
so the formula can change without a client release.

### Connecting

One person hosts the backend once. Either on their own Mac:

```bash
./selfhost-wizard.sh    # this Mac + SQLite + a Cloudflare Tunnel
```

or in the cloud, if you'd rather it stay up when that Mac sleeps:

```bash
./deploy-wizard.sh      # Fly.io + Neon Postgres
```

Both set up the GitHub OAuth app and print exactly what to send your friends.

Everyone else just points their own Claude HQ at it: **🏆 Arena → server URL →
Sign in with GitHub → paste the pairing code**. Your device token is stored in
`arena-link.json` (git-ignored) and is never exposed to the page.

Backend source, API and design notes: [`backend/README.md`](backend/README.md).

---

## 🎨 Customization

Open **⚙️ Settings** in the app for themes, creature packs, accent color, text size, calm mode,
refresh rate, stuck threshold, and daily budget. Press **?** in the app for the full keyboard-shortcut
and feature guide.

---

## Changelog

- **1.1.1** — Arena fixes: SSL error on python.org Python, a blank-dashboard startup crash
  (ARENA declared before first `setView`), and stopped hijacking browser shortcuts (thanks @hetnxik).
- **1.1.0** — Editable Trainer name (thanks @SwastikTripathi, #1), smart Insights engine,
  weekly digest, event log, project deep-dive, installable PWA, and a perf/dead-code hardening pass.
- **1.0.0** — Initial release: live fleet view, full-history search + transcript reader, analytics,
  daily digests, and the Pokémon-style collection / Gym / quests layer.

## Contributors

- [@SwastikTripathi](https://github.com/SwastikTripathi) — editable Trainer name setting (#1)

## License

MIT — see [LICENSE](LICENSE).

Pokémon names and sprites are the property of Nintendo / Game Freak / The Pokémon Company; the
"pokemon" creature pack hotlinks sprites from the public [PokéAPI](https://pokeapi.co/) sprite library
for personal use only. Use the built-in original "monsters" pack to avoid third-party assets entirely.
