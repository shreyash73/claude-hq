#!/usr/bin/env python3
"""
Claude Sessions Dashboard — a local, private web dashboard for your Claude Code sessions.

What it does
------------
Serves a small web app that shows your live Claude Code sessions (from
`claude agents --json`) enriched with data parsed from the JSONL transcript files
under ~/.claude/projects, plus a gamified "season" panel (XP / level / streak /
achievements / 14-day activity calendar) computed from the last 30 days of activity
across ALL sessions.

Endpoints
---------
  GET /               -> serves index.html (re-read from disk each request)
  GET /api/sessions   -> the JSON contract consumed by the frontend
  anything else       -> 404

Privacy stance
--------------
This is a LOCAL tool. It binds to 127.0.0.1 ONLY (never 0.0.0.0), and rejects any
request whose Host header is not localhost/127.0.0.1 with a 403. Your transcripts and
session activity never leave your machine. No auth is added beyond loopback binding
because the data is only exposed to processes on this host.

Usage
-----
  python3 dashboard.py [--port 8765] [--no-open]
"""

import argparse
import glob
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone, timedelta, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import arena

APP_VERSION = "1.2.0"

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# CSRF: a per-process token, injected into index.html (replacing the __HQ_CSRF__
# placeholder) so only a same-origin page can read it and echo it back on POSTs.
CSRF_TOKEN = secrets.token_hex(16)
CSRF_PLACEHOLDER = "__HQ_CSRF__"
SERVER_PORT = 8765  # set for real in main(); used for Origin validation

# launchd auto-start
LAUNCH_LABEL = "com.claudehq.dashboard"
LAUNCH_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/%s.plist" % LAUNCH_LABEL)

# Starter creatures — stable pick per sessionId via hash.
CREATURES = [
    ("⚡🐭", "Pikachu"),
    ("🔥🦎", "Charmander"),
    ("💧🐢", "Squirtle"),
    ("🌱🐸", "Bulbasaur"),
    ("💨🐉", "Dragonite"),
    ("💦🦆", "Psyduck"),
    ("🌙🦊", "Umbreon"),
    ("🪨🐛", "Geodude"),
]

# --------------------------------------------------------------------------- #
# ORIGINAL monster species (invented — NOT real Pokemon). Stable, ordered list
# of 48. A session maps to a species by hash(sessionId) % 48. The frontend draws
# a deterministic pixel sprite from seed + typeHue + stage + shiny.
# --------------------------------------------------------------------------- #
SPECIES_NAMES = [
    "Emberpup", "Aquafin", "Sprigling", "Voltkit", "Mystifox", "Boulderbug",
    "Frostnib", "Umbracat", "Pebblemol", "Cindermouse", "Tidewhorl", "Mossling",
    "Sparkfly", "Dreamowl", "Craghorn", "Glacimini", "Nocturnip", "Duskmoth",
    "Lumazee", "Brambeak", "Coralux", "Zaptadpole", "Gloamkit", "Terrapawn",
    "Flarelynx", "Marisprite", "Thornvale", "Ionbuzz", "Chronowisp", "Dracowyrm",
    "Rimepix", "Faewhisk",
    "Cindertail", "Brinewisp", "Fernbud", "Voltling", "Psybloom", "Stonecrag",
    "Frostmane", "Shadowpip", "Wispkit", "Drakelet", "Pixiewing", "Magmaturtle",
    "Rippletusk", "Vinecoil", "Sparkmoth", "Glimmerfawn",
]
# Fixed type per species (index-aligned with SPECIES_NAMES).
SPECIES_TYPES = [
    "fire", "water", "grass", "electric", "psychic", "rock",
    "ice", "shadow", "rock", "fire", "water", "grass",
    "electric", "psychic", "rock", "ice", "shadow", "shadow",
    "psychic", "grass", "water", "electric", "shadow", "normal",
    "fire", "water", "grass", "electric", "psychic", "dragon",
    "ice", "fairy",
    "fire", "water", "grass", "electric", "psychic", "rock",
    "ice", "shadow", "normal", "dragon", "fairy", "fire",
    "water", "grass", "electric", "fairy",
]
# Fixed hue (0..360) per type.
TYPE_HUES = {
    "fire": 10, "water": 205, "grass": 120, "electric": 52, "psychic": 285,
    "rock": 30, "ice": 190, "shadow": 265, "normal": 45,
    "dragon": 250, "fairy": 320,
}
# Evolution stages: reach stage i once promptCount >= STAGE_THRESHOLDS[i].
STAGE_THRESHOLDS = [0, 3, 12, 30, 70]
STAGE_NAMES = ["Egg", "Hatchling", "Juvenile", "Adult", "Apex"]


def species_seed(species):
    """Stable 24-bit sprite seed derived from the SPECIES index (not the session),
    so every session of a species shares one sprite pattern."""
    h = hashlib.sha256(("nymonster:species:%d" % int(species)).encode("utf-8"))
    return int(h.hexdigest(), 16) & 0xFFFFFF


def shiny_for_species(species):
    """Shiny is a stable PER-SPECIES property (~1 in 6 species), so every session of
    a shiny species is shiny — the Pokédex and the live cards always agree."""
    h = hashlib.sha256(("nymonster:shiny:%d" % int(species)).encode("utf-8"))
    return int(h.hexdigest(), 16) % 6 == 0


def stage_for(prompt_count):
    """Return (stage 0..4, stageName, stagePct 0..1) from a promptCount using
    STAGE_THRESHOLDS. stagePct is progress from this stage's threshold to the
    next (1.0 at the max stage)."""
    pc = int(prompt_count or 0)
    stage = 0
    for i, th in enumerate(STAGE_THRESHOLDS):
        if pc >= th:
            stage = i
    if stage >= len(STAGE_THRESHOLDS) - 1:
        pct = 1.0
    else:
        lo = STAGE_THRESHOLDS[stage]
        hi = STAGE_THRESHOLDS[stage + 1]
        pct = (pc - lo) / (hi - lo) if hi > lo else 1.0
        pct = max(0.0, min(1.0, pct))
    return stage, STAGE_NAMES[stage], round(float(pct), 4)

# Rank titles by level band.
RANKS = [
    (1, "Prompt Apprentice"),
    (3, "Prompt Adept"),
    (5, "Prompt Conjurer"),
    (8, "Prompt Sorcerer"),
    (12, "Prompt Archmage"),
    (18, "Prompt Ascendant"),
    (999, "Prompt Deity"),
]

# Prefixes that mean a "user" record is NOT a real human prompt.
_NON_HUMAN_PREFIXES = (
    "<command-",
    "<local-command",
    "<system-reminder",
    "Caveat:",
    "This session is being continued",
)

_PASTED_RE = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content>", re.DOTALL)
_ARTIFACT_RE = re.compile(r"https://claude\.ai/[^\s)\"']*artifact[^\s)\"']*")
_PR_RE = re.compile(r"https://github\.com/[^\s)\"']+/pull/(\d+)")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(s):
    """Parse an ISO8601 timestamp to an aware UTC datetime, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def strip_markdown(text):
    """Reduce markdown to readable plain text."""
    if not text:
        return ""
    t = text
    # links [label](url) -> label
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    # images ![alt](url) -> alt
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # code fences / inline backticks
    t = t.replace("```", " ")
    t = t.replace("`", "")
    # headings / list markers / emphasis at token level
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s{0,3}[-*+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = t.replace(">", "")
    # collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def clean_prompt(text):
    """Strip pasted-content noise and normalise whitespace for a human prompt."""
    if not text:
        return ""
    t = _PASTED_RE.sub(" [pasted content] ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_real_human_prompt(content):
    """A real human prompt: content is a string not starting with a noise prefix."""
    if not isinstance(content, str):
        return False
    s = content.lstrip()
    if not s:
        return False
    for p in _NON_HUMAN_PREFIXES:
        if s.startswith(p):
            return False
    return True


def truncate(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _session_hash(session_id):
    """The one canonical hash for a session id. species/shiny (and the pokedex
    mapping) all derive from THIS integer:
        h = int(sha256(sessionId).hexdigest(), 16)
        species = h % 48 ; shiny = (h // 48) % 16 == 0"""
    return int(hashlib.sha256((session_id or "").encode("utf-8")).hexdigest(), 16)


def creature_for(session_id, prompt_count=0):
    """Stable creature for a session. Keeps the legacy emoji/name/hue fields and
    ADDS the original-monster fields (species/type/seed/stage/shiny/...)."""
    h = _session_hash(session_id)
    emoji, name = CREATURES[h % len(CREATURES)]
    hue = h % 360
    species = h % 48
    shiny = shiny_for_species(species)   # per-species, so live cards == Pokédex
    species_name = SPECIES_NAMES[species]
    stype = SPECIES_TYPES[species]
    stage, stage_name, stage_pct = stage_for(prompt_count)
    return {
        "emoji": emoji, "name": name, "hue": hue,
        "species": int(species),
        "speciesName": species_name,
        "type": stype,
        "seed": int(species_seed(species)),
        "typeHue": int(TYPE_HUES.get(stype, 45)),
        "stage": int(stage),
        "stageName": stage_name,
        "stagePct": stage_pct,
        "shiny": bool(shiny),
    }


def rank_for_level(level):
    for threshold, title in RANKS:
        if level <= threshold:
            return title
    return RANKS[-1][1]


def xp_for_level(level):
    """XP needed to advance FROM the given level to the next."""
    return 400 + 120 * level


def derive_level(total_xp):
    """Walk cumulative thresholds from level 1. Returns (level, xp_into, xp_for)."""
    level = 1
    remaining = total_xp
    while True:
        need = xp_for_level(level)
        if remaining < need:
            return level, int(remaining), int(need)
        remaining -= need
        level += 1
        if level > 999:  # safety
            return level, int(remaining), int(xp_for_level(level))


def tool_label(tool_use):
    """Render a short human label for an in-flight tool_use block."""
    if not isinstance(tool_use, dict):
        return None
    name = tool_use.get("name") or "Tool"
    inp = tool_use.get("input") or {}
    detail = ""
    if isinstance(inp, dict):
        for key in ("description", "command", "file_path", "path", "pattern", "query", "url"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                detail = v.strip()
                break
    if detail:
        return truncate(f"{name}: {detail}", 80)
    return truncate(name, 80)


# --------------------------------------------------------------------------- #
# Transcript locating & parsing
# --------------------------------------------------------------------------- #

def find_transcript(session_id):
    """Locate the JSONL for a session id anywhere under projects."""
    if not session_id:
        return None
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session_id}.jsonl"))
    return matches[0] if matches else None


def _extract_links_from_text(text, links, seen):
    if not text:
        return
    for m in _ARTIFACT_RE.finditer(text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            links.append({"type": "artifact", "url": url, "label": "artifact"})
    for m in _PR_RE.finditer(text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            links.append({"type": "pr", "url": url, "label": f"PR #{m.group(1)}"})


# --------------------------------------------------------------------------- #
# Cost model (ESTIMATE). Prices per 1,000,000 tokens: (input, output)
# --------------------------------------------------------------------------- #

_PRICES = {
    "opus": (15.0, 75.0),
    "fable": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.8, 4.0),
}


def _price_for(model):
    """Return (input_price, output_price) per 1e6 tokens for a model string."""
    m = (model or "").lower()
    for key in ("opus", "fable", "sonnet", "haiku"):
        if key in m:
            return _PRICES[key]
    return _PRICES["sonnet"]  # unknown / empty -> sonnet pricing


def _usage_cost(model, usage):
    """Return (est_cost_usd, output, input, cache_read, cache_creation) for one record."""
    if not isinstance(usage, dict):
        return 0.0, 0, 0, 0, 0
    in_price, out_price = _price_for(model)
    it = int(usage.get("input_tokens") or 0)
    ot = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cc = int(usage.get("cache_creation_input_tokens") or 0)
    cost = (
        it * in_price
        + cr * in_price * 0.1
        + cc * in_price * 1.25
        + ot * out_price
    ) / 1_000_000.0
    return cost, ot, it, cr, cc


# Error signatures that mean a session needs attention (case-insensitive).
_ERROR_SIGS = (
    "organization has disabled", "disabled claude", "rate limit", "overloaded",
    "invalid api key", "credit balance", "billing", "quota", "insufficient",
    "authentication_error", "permission denied by user",
)


def _match_error(text):
    if not text:
        return None
    low = text.lower()
    for sig in _ERROR_SIGS:
        if sig in low:
            return sig
    return None


# --------------------------------------------------------------------------- #
# Single-pass per-file scan, with an mtime/size cache.
#
# scan_file(path) returns a rich per-file aggregate that feeds BOTH the season
# scan and the per-session views in ONE pass. A file is re-read only when its
# (mtime, size) changed; otherwise the cached aggregate is reused.
# --------------------------------------------------------------------------- #

_scan_cache = {}
_scan_lock = threading.Lock()


def _new_day():
    return {
        "prompts": 0, "tools": 0, "artifacts": 0, "replies": 0,
        "tools_by_name": {}, "hours": {},
        "output": 0, "input": 0, "cacheRead": 0, "cacheCreation": 0, "cost": 0.0,
    }


def scan_file(path):
    """Return the cached per-file aggregate, re-reading only if mtime/size changed."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except Exception:
        return None
    key = (st.st_mtime, st.st_size)
    with _scan_lock:
        cached = _scan_cache.get(path)
        if cached is not None and cached.get("_key") == key:
            return cached
    agg = _scan_file_uncached(path)
    if agg is not None:
        agg["_key"] = key
        with _scan_lock:
            _scan_cache[path] = agg
    return agg


def _scan_file_uncached(path):
    """Read one transcript once, producing everything downstream views need."""
    agg = {
        "ai_title": None, "last_prompt": None, "last_reply": None, "now_label": None,
        "first_prompt": None, "prompt_count": 0, "last_activity": None, "links": [],
        "folder": os.path.basename(os.path.dirname(path)),
        "per_day": {}, "activity_ts": [], "errors": [], "timeline": [], "files": {},
        "model": "", "tok_output": 0, "tok_input": 0, "tok_cacheRead": 0,
        "tok_cacheCreation": 0, "cost": 0.0,
    }
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return agg

    seen_links = set()
    last_assistant_text = None
    last_assistant_tool = None

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            try:
                typ = o.get("type")
                ts = parse_ts(o.get("timestamp"))
                diso = ts.date().isoformat() if ts else None
                lhour = ts.astimezone().hour if ts else None

                if typ == "ai-title":
                    at = o.get("aiTitle")
                    if at:
                        agg["ai_title"] = at

                elif typ == "last-prompt":
                    lp = o.get("lastPrompt")
                    if lp:
                        agg["last_prompt"] = clean_prompt(lp)

                elif typ == "user":
                    content = (o.get("message") or {}).get("content")
                    if is_real_human_prompt(content):
                        cleaned = clean_prompt(content)
                        agg["prompt_count"] += 1
                        if agg["first_prompt"] is None:
                            agg["first_prompt"] = cleaned
                        if ts and (agg["last_activity"] is None or ts > agg["last_activity"]):
                            agg["last_activity"] = ts
                        _extract_links_from_text(content, agg["links"], seen_links)
                        if diso:
                            d = agg["per_day"].setdefault(diso, _new_day())
                            d["prompts"] += 1
                            for _ in _ARTIFACT_RE.finditer(content):
                                d["artifacts"] += 1
                            if lhour is not None:
                                d["hours"][lhour] = d["hours"].get(lhour, 0) + 1
                        if ts is not None:
                            agg["activity_ts"].append(ts.timestamp())
                            agg["timeline"].append({
                                "t": ts.isoformat(), "kind": "you",
                                "text": truncate(cleaned, 200), "tool": None,
                            })

                elif typ == "assistant":
                    msg = o.get("message") or {}
                    blocks = msg.get("content")
                    model = msg.get("model") or ""
                    if model:
                        agg["model"] = model
                    cost, ot, it, cr, cc = _usage_cost(model, msg.get("usage") or {})
                    agg["cost"] += cost
                    agg["tok_output"] += ot
                    agg["tok_input"] += it
                    agg["tok_cacheRead"] += cr
                    agg["tok_cacheCreation"] += cc
                    d = agg["per_day"].setdefault(diso, _new_day()) if diso else None
                    if d is not None:
                        d["cost"] += cost
                        d["output"] += ot
                        d["input"] += it
                        d["cacheRead"] += cr
                        d["cacheCreation"] += cc
                    if isinstance(blocks, list):
                        texts = []
                        last_tool = None
                        for b in blocks:
                            if not isinstance(b, dict):
                                continue
                            bt = b.get("type")
                            if bt == "text":
                                txt = b.get("text") or ""
                                texts.append(txt)
                                _extract_links_from_text(txt, agg["links"], seen_links)
                                sig = _match_error(txt)
                                if sig and ts is not None:
                                    agg["errors"].append((ts.timestamp(), sig))
                                if d is not None:
                                    for _ in _ARTIFACT_RE.finditer(txt):
                                        d["artifacts"] += 1
                            elif bt == "tool_use":
                                last_tool = b
                                name = b.get("name") or "Tool"
                                if d is not None:
                                    d["tools"] += 1
                                    d["tools_by_name"][name] = d["tools_by_name"].get(name, 0) + 1
                                    if lhour is not None:
                                        d["hours"][lhour] = d["hours"].get(lhour, 0) + 1
                                if ts is not None:
                                    agg["activity_ts"].append(ts.timestamp())
                                    agg["timeline"].append({
                                        "t": ts.isoformat(), "kind": "tool",
                                        "text": tool_label(b) or name, "tool": name,
                                    })
                                inp = b.get("input") or {}
                                p = inp.get("file_path") or inp.get("path") if isinstance(inp, dict) else None
                                if isinstance(p, str) and p.strip():
                                    nl = name.lower()
                                    if nl == "write":
                                        action = "write"
                                    elif "edit" in nl:
                                        action = "edit"
                                    elif nl == "read":
                                        action = "read"
                                    else:
                                        action = "other"
                                    fe = agg["files"].setdefault(
                                        p, {"path": p, "action": action, "count": 0})
                                    fe["count"] += 1
                        if texts:
                            last_assistant_text = "\n".join(texts)
                            if d is not None:
                                d["replies"] += 1
                                if lhour is not None:
                                    d["hours"][lhour] = d["hours"].get(lhour, 0) + 1
                            if ts is not None:
                                agg["activity_ts"].append(ts.timestamp())
                                agg["timeline"].append({
                                    "t": ts.isoformat(), "kind": "claude",
                                    "text": truncate(strip_markdown(last_assistant_text), 200),
                                    "tool": None,
                                })
                        if last_tool is not None:
                            last_assistant_tool = last_tool
                    if ts and (agg["last_activity"] is None or ts > agg["last_activity"]):
                        agg["last_activity"] = ts

                elif typ == "system":
                    content = o.get("content")
                    if isinstance(content, str):
                        sig = _match_error(content)
                        if sig and ts is not None:
                            agg["errors"].append((ts.timestamp(), sig))
                        if "claude.ai" in content or "github.com" in content:
                            _extract_links_from_text(content, agg["links"], seen_links)

                else:
                    if "claude.ai" in line or "github.com" in line:
                        _extract_links_from_text(line, agg["links"], seen_links)
            except Exception:
                # never crash on a single record
                continue

    if last_assistant_text:
        agg["last_reply"] = strip_markdown(last_assistant_text)
    if last_assistant_tool is not None:
        agg["now_label"] = tool_label(last_assistant_tool)
    agg["links"] = agg["links"][:4]
    if len(agg["timeline"]) > 60:
        agg["timeline"] = agg["timeline"][-60:]
    return agg


def parse_transcript(path):
    """Back-compat wrapper: same shape as before, now backed by the file cache."""
    agg = scan_file(path)
    if agg is None:
        return {
            "ai_title": None, "last_prompt": None, "last_reply": None, "now_label": None,
            "first_prompt": None, "prompt_count": 0, "last_activity": None, "links": [],
        }
    return {
        "ai_title": agg["ai_title"], "last_prompt": agg["last_prompt"],
        "last_reply": agg["last_reply"], "now_label": agg["now_label"],
        "first_prompt": agg["first_prompt"], "prompt_count": agg["prompt_count"],
        "last_activity": agg["last_activity"], "links": agg["links"][:4],
    }


def _session_tokens(agg):
    """Token/cost summary for a single transcript aggregate."""
    out = agg.get("tok_output", 0)
    inp = agg.get("tok_input", 0)
    cr = agg.get("tok_cacheRead", 0)
    total = out + inp + cr + agg.get("tok_cacheCreation", 0)
    return {
        "output": int(out), "input": int(inp), "cacheRead": int(cr),
        "total": int(total), "estCostUSD": round(agg.get("cost", 0.0), 4),
        "model": agg.get("model", "") or "",
    }


def _buckets_from_ts(activity_ts, n_buckets, span_secs):
    """Bucket activity timestamps over the last span_secs into n_buckets (oldest->newest)."""
    now = now_utc().timestamp()
    start = now - span_secs
    width = span_secs / n_buckets
    buckets = [0] * n_buckets
    for ts in activity_ts:
        if ts < start or ts > now:
            continue
        idx = int((ts - start) / width)
        if idx < 0:
            idx = 0
        elif idx >= n_buckets:
            idx = n_buckets - 1
        buckets[idx] += 1
    return buckets


# --------------------------------------------------------------------------- #
# Season stats (30-day scan of ALL transcripts)
# --------------------------------------------------------------------------- #

def compute_season():
    """
    Scan all *.jsonl under projects, counting per-day real prompts, tool_use blocks,
    and artifact links over the last 30 days. Build XP / level / streak / calendar /
    achievements / totals.
    """
    today = now_utc().date()
    window_start = today - timedelta(days=29)  # 30-day inclusive window

    # per-day tallies
    day_prompts = {}   # date -> count
    day_tools = {}     # date -> count
    day_artifacts = {} # date -> count
    day_replies = {}   # date -> count (assistant records)
    day_cost = {}      # date -> est USD
    active_dates = set()
    folders = set()

    # extended insights (30-day window)
    tok_out = tok_in = tok_cr = tok_cc = 0
    total_cost = 0.0
    tool_counts = {}
    folder_prompts = {}
    folder_tools = {}
    hourly = [0] * 24
    night_owl = False

    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    for path in files:
        agg = scan_file(path)
        if agg is None:
            continue
        fol = agg.get("folder") or ""
        folders.add(fol)
        for diso, dd in agg.get("per_day", {}).items():
            try:
                d = date.fromisoformat(diso)
            except Exception:
                continue
            if d < window_start or d > today:
                continue
            p = dd["prompts"]; t = dd["tools"]; a = dd["artifacts"]; r = dd["replies"]
            if p:
                day_prompts[d] = day_prompts.get(d, 0) + p
                active_dates.add(d)
                folder_prompts[fol] = folder_prompts.get(fol, 0) + p
            if t:
                day_tools[d] = day_tools.get(d, 0) + t
                folder_tools[fol] = folder_tools.get(fol, 0) + t
            if a:
                day_artifacts[d] = day_artifacts.get(d, 0) + a
            if r:
                day_replies[d] = day_replies.get(d, 0) + r
                active_dates.add(d)
            day_cost[d] = day_cost.get(d, 0.0) + dd["cost"]
            tok_out += dd["output"]; tok_in += dd["input"]
            tok_cr += dd["cacheRead"]; tok_cc += dd["cacheCreation"]
            total_cost += dd["cost"]
            for nm, c in dd["tools_by_name"].items():
                tool_counts[nm] = tool_counts.get(nm, 0) + c
            for h, c in dd["hours"].items():
                if 0 <= h < 24:
                    hourly[h] += c
                    if h <= 5 and c > 0:
                        night_owl = True

    total_prompts = sum(day_prompts.values())
    total_tools = sum(day_tools.values())
    total_artifacts = sum(day_artifacts.values())
    active_days = len(active_dates)

    xp = total_prompts * 10 + total_tools * 3 + total_artifacts * 40
    level, xp_into, xp_for = derive_level(xp)
    pct = round((xp_into / xp_for) * 100.0, 1) if xp_for else 0.0

    # streak: consecutive active days ending today or yesterday
    streak = 0
    if today in active_dates:
        cur = today
    elif (today - timedelta(days=1)) in active_dates:
        cur = today - timedelta(days=1)
    else:
        cur = None
    if cur is not None:
        while cur in active_dates:
            streak += 1
            cur = cur - timedelta(days=1)

    # best streak within the window
    best = 0
    run = 0
    prev = None
    for d in sorted(active_dates):
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        best = max(best, run)
        prev = d
    best_streak = max(best, streak)

    # calendar: last 14 days oldest -> newest
    calendar = []
    counts_for_heat = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        c = day_prompts.get(d, 0) + day_replies.get(d, 0)
        counts_for_heat.append(c)
    maxc = max(counts_for_heat) if counts_for_heat else 0
    for idx, i in enumerate(range(13, -1, -1)):
        d = today - timedelta(days=i)
        c = counts_for_heat[idx]
        if c <= 0:
            heat = 0
        elif maxc <= 0:
            heat = 0
        else:
            frac = c / maxc
            if frac >= 0.75:
                heat = 4
            elif frac >= 0.5:
                heat = 3
            elif frac >= 0.25:
                heat = 2
            else:
                heat = 1
        calendar.append({
            "date": d.isoformat(),
            "count": c,
            "heat": heat,
            "today": d == today,
        })

    # night owl: computed in the single pass above (any local-hour 0..5 activity).

    distinct_folders = len(folders)

    achievements = [
        _ach("century", "Century", "100+ prompts in 30 days", "💯",
             total_prompts, 100),
        _ach("artificer", "Artificer", "10+ artifacts in 30 days", "🎨",
             total_artifacts, 10),
        _ach("streak_keeper", "Streak Keeper", "5+ day streak", "🔥",
             streak, 5),
        _ach("tool_smith", "Tool Smith", "500+ tool calls in 30 days", "🛠️",
             total_tools, 500),
        _ach("polyglot", "Polyglot", "Worked in 5+ distinct folders", "🌐",
             distinct_folders, 5),
        _ach("marathon", "Marathoner", "Active on 20+ days", "🏃",
             active_days, 20),
        {
            "id": "night_owl", "name": "Night Owl",
            "desc": "Coded between midnight and 5am",
            "icon": "🦉",
            "unlocked": bool(night_owl),
            "progress": 1.0 if night_owl else 0.0,
        },
        _ach("power_user", "Power User", "1000+ XP in 30 days", "⚡",
             xp, 1000),
    ]

    # --- extended insights (all additive) ---
    tokens = {
        "output": int(tok_out), "input": int(tok_in), "cacheRead": int(tok_cr),
        "total": int(tok_out + tok_in + tok_cr + tok_cc),
        "estCostUSD": round(total_cost, 2),
    }
    tool_breakdown = sorted(
        ({"name": nm, "count": int(c)} for nm, c in tool_counts.items()),
        key=lambda x: -x["count"],
    )[:8]
    fl = []
    for f2 in set(list(folder_prompts) + list(folder_tools)):
        pp = folder_prompts.get(f2, 0)
        tt = folder_tools.get(f2, 0)
        fl.append({"folder": f2, "prompts": int(pp), "tools": int(tt),
                   "score": int(pp * 10 + tt)})
    fl.sort(key=lambda x: -x["score"])
    folder_leaderboard = fl[:6]
    daily_cost = []
    for i in range(13, -1, -1):
        dd2 = today - timedelta(days=i)
        daily_cost.append({"date": dd2.isoformat(), "usd": round(day_cost.get(dd2, 0.0), 4)})

    return {
        "level": level,
        "xp": int(xp),
        "xpIntoLevel": xp_into,
        "xpForLevel": xp_for,
        "pct": pct,
        "rank": rank_for_level(level),
        "streak": int(streak),
        "bestStreak": int(best_streak),
        "totals": {
            "prompts": int(total_prompts),
            "tools": int(total_tools),
            "artifacts": int(total_artifacts),
            "activeDays": int(active_days),
        },
        "calendar": calendar,
        "achievements": achievements,
        "tokens": tokens,
        "toolBreakdown": tool_breakdown,
        "folderLeaderboard": folder_leaderboard,
        "hourly": hourly,
        "dailyCost": daily_cost,
    }


def _ach(aid, name, desc, icon, actual, target):
    progress = min(1.0, actual / target) if target else 0.0
    return {
        "id": aid, "name": name, "desc": desc, "icon": icon,
        "unlocked": progress >= 1.0,
        "progress": round(progress, 3),
    }


# --------------------------------------------------------------------------- #
# Live sessions
# --------------------------------------------------------------------------- #

def _find_claude():
    """Locate the `claude` binary. Under launchd the PATH is minimal, so we
    check common install locations by absolute path before falling back to PATH."""
    import shutil
    cands = [
        os.path.expanduser("~/.claude/local/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.npm-global/bin/claude"),
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    aug = os.environ.get("PATH", "") + \
        ":/opt/homebrew/bin:/usr/local/bin:" + os.path.expanduser("~/.local/bin")
    return shutil.which("claude", path=aug) or "claude"


def get_live_agents():
    """Return (agents_list, error_or_None)."""
    try:
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + \
            ":/opt/homebrew/bin:/usr/local/bin:" + os.path.expanduser("~/.local/bin")
        proc = subprocess.run(
            [_find_claude(), "agents", "--json"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if proc.returncode != 0:
            return [], f"claude agents exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        data = json.loads(proc.stdout)
        if isinstance(data, list):
            return data, None
        return [], "unexpected output shape from claude agents --json"
    except FileNotFoundError:
        return [], "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return [], "claude agents --json timed out"
    except Exception as e:
        return [], f"claude agents error: {e}"


def build_session(agent):
    """Build one session object from a live agent + its transcript."""
    session_id = agent.get("sessionId") or agent.get("id") or ""
    short_id = (session_id or "")[:8] or "unknown"
    cwd = agent.get("cwd") or ""
    folder = os.path.basename(cwd.rstrip("/")) if cwd else ""
    kind = agent.get("kind") or "interactive"

    raw_status = ""
    if kind == "background":
        raw_status = agent.get("state") or ""
    else:
        raw_status = agent.get("status") or ""

    # status mapping
    if kind == "interactive":
        if raw_status == "busy":
            status = "working"
        elif raw_status == "idle":
            status = "idle"
        else:
            status = "idle"
    else:  # background
        if raw_status == "blocked":
            status = "needs"
        else:
            status = "idle"

    path = find_transcript(session_id)
    agg = scan_file(path)
    tx = parse_transcript(path)  # cached; cheap

    # last activity: prefer transcript; else startedAt
    last_activity_dt = tx["last_activity"]
    if last_activity_dt is None:
        started = agent.get("startedAt")
        if isinstance(started, (int, float)):
            last_activity_dt = datetime.fromtimestamp(started / 1000.0, tz=timezone.utc)

    if last_activity_dt is not None:
        age_secs = int((now_utc() - last_activity_dt).total_seconds())
        last_activity_iso = last_activity_dt.isoformat()
    else:
        age_secs = 0
        last_activity_iso = ""

    stale = age_secs > 86400
    if stale and status != "working":
        status = "stale"

    # --- alert detection: recent (age < 6h) error override, else blocked note ---
    alert = None
    alert_kind = None
    errors = agg.get("errors", []) if isinstance(agg, dict) else []
    cutoff = now_utc().timestamp() - 6 * 3600
    recent = None
    for ts, sig in errors:
        if ts >= cutoff and (recent is None or ts > recent[0]):
            recent = (ts, sig)
    if recent is not None:
        alert = 'Recent error detected: matched "%s" in session output' % recent[1]
        alert_kind = "error"
        status = "needs"  # force attention regardless of prior status
    elif status == "needs":
        alert = "Background agent is blocked / awaiting input"
        alert_kind = "blocked"

    now_label = tx["now_label"] if status == "working" else None

    name = agent.get("name") or short_id
    title = tx["ai_title"] or "Untitled session"

    if isinstance(agg, dict) and agg:
        tokens = _session_tokens(agg)
        spark = _buckets_from_ts(agg.get("activity_ts", []), 12, 24 * 3600)
    else:
        tokens = {"output": 0, "input": 0, "cacheRead": 0, "total": 0,
                  "estCostUSD": 0.0, "model": ""}
        spark = [0] * 12

    return {
        "id": short_id,
        "sessionId": session_id,
        "name": name,
        "title": title,
        "cwd": cwd,
        "folder": folder,
        "kind": kind,
        "pid": agent.get("pid") if isinstance(agent.get("pid"), int) else None,
        "status": status,
        "rawStatus": raw_status,
        "creature": creature_for(session_id, tx["prompt_count"]),
        "firstPrompt": tx["first_prompt"] or "",
        "lastPrompt": tx["last_prompt"] or (tx["first_prompt"] or ""),
        "lastReply": tx["last_reply"] or "",
        "now": now_label,
        "promptCount": tx["prompt_count"],
        "lastActivity": last_activity_iso,
        "ageSecs": age_secs,
        "stale": stale,
        "links": tx["links"],
        "alert": alert,
        "alertKind": alert_kind,
        "tokens": tokens,
        "spark": spark,
        # session-meta (merged from sessions-meta.json in build_payload)
        "pinned": False,
        "tags": [],
        "note": "",
        # stuck detection (filled in build_payload using config.stuckMinutes)
        "stuck": False,
        "stuckReason": None,
    }


def build_feed(sessions, limit=25):
    """Build a merged recent-activity feed across the LIVE sessions (newest first)."""
    events = []
    for s in sessions:
        sid = s.get("sessionId")
        if not sid:
            continue
        try:
            path = find_transcript(sid)
            agg = scan_file(path)
        except Exception:
            agg = None
        if not isinstance(agg, dict):
            continue
        title = s.get("title") or agg.get("ai_title") or "Untitled session"
        for ev in agg.get("timeline", [])[-12:]:
            k = ev.get("kind")
            if k not in ("you", "claude", "tool"):
                continue
            events.append({
                "t": ev.get("t"),
                "sessionId": sid,
                "title": title,
                "kind": k,
                "text": ev.get("text") or "",
                "tool": ev.get("tool"),
            })
        if s.get("alert"):
            events.append({
                "t": s.get("lastActivity") or "",
                "sessionId": sid,
                "title": title,
                "kind": "needs",
                "text": s.get("alert"),
                "tool": None,
            })
    events.sort(key=lambda e: _epoch(e.get("t")), reverse=True)
    return events[:limit]


_ARCHIVED_CAP = 150  # most-recent archived transcripts to surface as stale cards


def build_archived_session(path, sid):
    """Build a stale 'card' for a past (non-live) transcript, mirroring build_session."""
    agg = scan_file(path) or {}
    la = agg.get("last_activity")
    age = int((now_utc() - la).total_seconds()) if la else 0
    if isinstance(agg, dict) and agg:
        tokens = _session_tokens(agg)
        spark = _buckets_from_ts(agg.get("activity_ts", []), 12, 24 * 3600)
    else:
        tokens = {"output": 0, "input": 0, "cacheRead": 0, "total": 0,
                  "estCostUSD": 0.0, "model": ""}
        spark = [0] * 12
    return {
        "id": (sid or "")[:8] or "unknown", "sessionId": sid,
        "name": (sid or "")[:8] or "archived",
        "title": agg.get("ai_title") or "Untitled session",
        "cwd": "", "folder": agg.get("folder") or "", "kind": "archived", "pid": None,
        "status": "stale", "rawStatus": "archived",
        "creature": creature_for(sid, agg.get("prompt_count", 0)),
        "firstPrompt": agg.get("first_prompt") or "",
        "lastPrompt": agg.get("last_prompt") or agg.get("first_prompt") or "",
        "lastReply": agg.get("last_reply") or "",
        "now": None, "promptCount": agg.get("prompt_count", 0),
        "lastActivity": la.isoformat() if la else "", "ageSecs": age,
        "stale": True, "links": (agg.get("links") or [])[:4],
        "alert": None, "alertKind": None, "tokens": tokens, "spark": spark,
        "archived": True,
    }


def build_payload():
    agents, error = get_live_agents()
    sessions = []
    for a in agents:
        try:
            sessions.append(build_session(a))
        except Exception:
            # never let one bad agent crash the whole payload
            continue

    # --- archived sessions: every past transcript (not currently live) as a stale card ---
    try:
        live_ids = set(s.get("sessionId") for s in sessions)
        arch = []
        for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
            sid = _session_id_from_path(p)
            if not sid or sid in live_ids:
                continue
            agg = scan_file(p)
            if not agg or agg.get("prompt_count", 0) < 1:
                continue
            la = agg.get("last_activity")
            arch.append((la.timestamp() if la else 0.0, p, sid))
        arch.sort(key=lambda x: -x[0])
        for _, p, sid in arch[:_ARCHIVED_CAP]:
            try:
                sessions.append(build_archived_session(p, sid))
            except Exception:
                continue
    except Exception:
        pass

    # sort: working first, then needs, then idle, then stale; newest activity first
    order = {"working": 0, "needs": 1, "idle": 2, "stale": 3}
    sessions.sort(key=lambda s: (order.get(s["status"], 9), -_epoch(s["lastActivity"])))

    try:
        season = compute_season()
    except Exception as e:
        season = _empty_season(str(e))

    try:
        feed = build_feed(sessions)
    except Exception:
        feed = []

    # --- config + session-meta merge + health (all additive) ---
    try:
        config = load_config()
    except Exception:
        config = dict(DEFAULT_CONFIG)
    try:
        meta = load_meta()
    except Exception:
        meta = {}

    stuck_secs = int(config.get("stuckMinutes", 15)) * 60
    stuck_ids = []
    for s in sessions:
        sid = s.get("sessionId")
        m = meta.get(sid)
        if m:
            s["pinned"] = bool(m.get("pinned"))
            s["tags"] = list(m.get("tags") or [])
            s["note"] = m.get("note") or ""
            alias = (m.get("name") or "").strip()
            if alias:
                s["alias"] = alias
                s["autoTitle"] = s.get("title")  # keep the original for reference
                s["title"] = alias               # rename throughout HQ
        if s.get("status") == "working" and int(s.get("ageSecs", 0)) > stuck_secs:
            mins = int(s.get("ageSecs", 0)) // 60
            s["stuck"] = True
            s["stuckReason"] = "No activity for %d min while working" % mins
            if sid:
                stuck_ids.append(sid)

    # today's list-price cost estimate: reuse season's dailyCost (last = today)
    daily_cost = 0.0
    try:
        dc = season.get("dailyCost") or []
        if dc:
            daily_cost = float(dc[-1].get("usd", 0.0) or 0.0)
    except Exception:
        daily_cost = 0.0

    budget = float(config.get("dailyBudgetUSD", 0) or 0)
    over_budget = bool(budget > 0 and daily_cost > budget)
    budget_pct = round(daily_cost / budget, 4) if budget > 0 else 0.0
    warnings = []
    if stuck_ids:
        warnings.append("%d tab%s may be stuck"
                        % (len(stuck_ids), "" if len(stuck_ids) == 1 else "s"))
    if over_budget:
        warnings.append("over daily budget")

    health = {
        "stuck": stuck_ids,
        "stuckCount": len(stuck_ids),
        "dailyCostUSD": round(daily_cost, 4),
        "dailyBudgetUSD": budget,
        "overBudget": over_budget,
        "budgetPct": budget_pct,
        "warnings": warnings,
    }

    payload = {
        "updated": now_utc().isoformat(),
        "version": APP_VERSION,
        "season": season,
        "sessions": sessions,
        "feed": feed,
        "health": health,
        "config": config,
    }
    if error:
        payload["error"] = error
    return payload


def _epoch(iso):
    dt = parse_ts(iso)
    return dt.timestamp() if dt else 0.0


# Short whole-payload memo so bursts of requests don't recompute everything.
_payload_memo = {"ts": 0.0, "data": None}
_payload_memo_lock = threading.Lock()


def build_payload_memo():
    now = time.monotonic()
    with _payload_memo_lock:
        if _payload_memo["data"] is not None and (now - _payload_memo["ts"]) < 1.5:
            return _payload_memo["data"]
    data = build_payload()
    with _payload_memo_lock:
        _payload_memo["ts"] = time.monotonic()
        _payload_memo["data"] = data
    return data


def build_session_detail(sid):
    """Build the single-session detail payload, or None if unknown."""
    path = find_transcript(sid)
    if not path:
        return None
    agg = scan_file(path)
    if agg is None:
        return None

    # Reuse live-derived fields (status/kind/cwd/links/title) when the session is live.
    try:
        payload = build_payload_memo()
        sess = next((s for s in payload.get("sessions", [])
                     if s.get("sessionId") == sid), None)
    except Exception:
        sess = None

    if sess:
        folder = sess.get("folder", "") or agg["folder"]
        cwd = sess.get("cwd", "")
        kind = sess.get("kind", "interactive")
        status = sess.get("status", "idle")
        links = sess.get("links", []) or agg["links"][:4]
        title = sess.get("title") or (agg["ai_title"] or "Untitled session")
    else:
        folder = agg["folder"]
        cwd = ""
        kind = "interactive"
        links = agg["links"][:4]
        title = agg["ai_title"] or "Untitled session"
        la = agg["last_activity"]
        age = int((now_utc() - la).total_seconds()) if la else 0
        status = "stale" if age > 86400 else "idle"
        cutoff = now_utc().timestamp() - 6 * 3600
        if any(ts >= cutoff for ts, _ in agg["errors"]):
            status = "needs"

    files = sorted(agg["files"].values(), key=lambda x: -x["count"])[:15]

    # timing/history metadata
    ts_list = agg.get("activity_ts", []) or []
    first_iso = ""
    span_days = 0
    if ts_list:
        first_ts = min(ts_list)
        last_ts = max(ts_list)
        first_iso = datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat()
        span_days = int((last_ts - first_ts) // 86400)
    active_days = len(agg.get("per_day", {}) or {})

    return {
        "id": (sid or "")[:8] or "unknown",
        "sessionId": sid,
        "title": title,
        "folder": folder,
        "cwd": cwd,
        "kind": kind,
        "status": status,
        "model": agg["model"] or "",
        "tokens": _session_tokens(agg),
        "timeline": agg["timeline"][-20:],
        "files": files,
        "links": links,
        "sparkHourly": _buckets_from_ts(agg.get("activity_ts", []), 24, 24 * 3600),
        "resumeCmd": "claude --resume %s" % sid,
        "lastReplyFull": agg["last_reply"] or "",
        "firstActivity": first_iso,
        "lastActivity": (agg["last_activity"].isoformat() if agg.get("last_activity") else ""),
        "spanDays": span_days,
        "activeDays": active_days,
        "promptCount": agg.get("prompt_count", 0),
    }


def _empty_season(err=None):
    today = now_utc().date()
    calendar = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        calendar.append({"date": d.isoformat(), "count": 0, "heat": 0, "today": d == today})
    daily_cost = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        daily_cost.append({"date": d.isoformat(), "usd": 0.0})
    s = {
        "level": 1, "xp": 0, "xpIntoLevel": 0, "xpForLevel": xp_for_level(1),
        "pct": 0.0, "rank": rank_for_level(1), "streak": 0, "bestStreak": 0,
        "totals": {"prompts": 0, "tools": 0, "artifacts": 0, "activeDays": 0},
        "calendar": calendar, "achievements": [],
        "tokens": {"output": 0, "input": 0, "cacheRead": 0, "total": 0, "estCostUSD": 0.0},
        "toolBreakdown": [], "folderLeaderboard": [], "hourly": [0] * 24,
        "dailyCost": daily_cost,
    }
    if err:
        s["error"] = err
    return s


# --------------------------------------------------------------------------- #
# Full-history search index (cached by file mtime/size), over ALL transcripts.
# --------------------------------------------------------------------------- #

_search_cache = {}
_search_lock = threading.Lock()
_SEARCH_MAX_BLOB = 200_000  # cap stored text per file to keep memory bounded


def _session_id_from_path(path):
    base = os.path.basename(path)
    return base[:-6] if base.endswith(".jsonl") else base


def _build_search_entry(path):
    """Read one transcript, collecting ai-title + human prompts + assistant text."""
    title = None
    last_activity = None
    parts = []
    total = 0
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return None
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            try:
                typ = o.get("type")
                ts = parse_ts(o.get("timestamp"))
                if ts and (last_activity is None or ts > last_activity):
                    last_activity = ts
                if total >= _SEARCH_MAX_BLOB:
                    continue
                if typ == "ai-title":
                    at = o.get("aiTitle")
                    if at:
                        title = at
                        parts.append(at)
                        total += len(at)
                elif typ == "user":
                    content = (o.get("message") or {}).get("content")
                    if is_real_human_prompt(content):
                        c = clean_prompt(content)
                        parts.append(c)
                        total += len(c)
                elif typ == "assistant":
                    blocks = (o.get("message") or {}).get("content")
                    if isinstance(blocks, list):
                        for b in blocks:
                            if isinstance(b, dict) and b.get("type") == "text":
                                t = b.get("text") or ""
                                if t:
                                    st = strip_markdown(t)
                                    parts.append(st)
                                    total += len(st)
            except Exception:
                continue
    text = " \n ".join(parts)[:_SEARCH_MAX_BLOB]
    return {
        "sessionId": _session_id_from_path(path),
        "title": title or "Untitled session",
        "folder": os.path.basename(os.path.dirname(path)),
        "lastActivity": last_activity.isoformat() if last_activity else "",
        "text": text,
        "blob": text.lower(),
    }


def get_search_entry(path):
    """Cached search entry for one file, rebuilt only when (mtime,size) changed."""
    try:
        st = os.stat(path)
    except Exception:
        return None
    key = (st.st_mtime, st.st_size)
    with _search_lock:
        cached = _search_cache.get(path)
        if cached is not None and cached.get("_key") == key:
            return cached
    entry = _build_search_entry(path)
    if entry is not None:
        entry["_key"] = key
        with _search_lock:
            _search_cache[path] = entry
    return entry


def _live_status_map():
    """Map sessionId -> live status, from the memoized live payload."""
    out = {}
    try:
        payload = build_payload_memo()
        for s in payload.get("sessions", []):
            sid = s.get("sessionId")
            if sid:
                out[sid] = s.get("status", "idle")
    except Exception:
        pass
    return out


_TERM_RE = re.compile(r"[a-z0-9_]+")


def _snippet_around(text, idx, term_len):
    start = max(0, idx - 70)
    end = min(len(text), idx + term_len + 90)
    snip = re.sub(r"\s+", " ", text[start:end]).strip()
    if start > 0:
        snip = "…" + snip
    if end < len(text):
        snip = snip + "…"
    return snip


def search_transcripts(q, limit=40):
    """TF-IDF ranked search across ALL transcripts. Query is split into terms;
    documents are scored by sum(tf * idf) so rarer terms weigh more and docs
    matching more of the query rank higher. Response shape is unchanged."""
    ql = (q or "").lower()
    terms = _TERM_RE.findall(ql)
    if not terms:
        # fall back to a raw substring if the query has no word chars
        terms = [ql.strip()] if ql.strip() else []
    terms = list(dict.fromkeys(terms))  # dedupe, keep order
    if not terms:
        return []

    live_map = _live_status_map()

    # First pass: gather candidate entries + per-term document frequencies.
    entries = []
    df = {t: 0 for t in terms}
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        entry = get_search_entry(path)
        if entry is None:
            continue
        blob = entry["blob"]
        tf = {}
        for t in terms:
            c = blob.count(t)
            if c:
                tf[t] = c
                df[t] += 1
        if tf:
            entries.append((entry, tf))

    n_docs = max(1, len(entries))
    import math
    idf = {t: math.log(1.0 + n_docs / (1.0 + df[t])) for t in terms}

    results = []
    for entry, tf in entries:
        score = 0.0
        matches = 0
        for t, c in tf.items():
            score += (1.0 + math.log(c)) * idf[t]
            matches += c
        # bonus for covering more distinct query terms
        score *= (1.0 + 0.5 * (len(tf) - 1))

        blob = entry["blob"]
        text = entry["text"]
        # snippet around the first occurrence of the rarest matched term
        best_t = min(tf.keys(), key=lambda t: (df[t], -len(t)))
        idx = blob.find(best_t)
        if idx < 0:
            idx = 0
        sid = entry["sessionId"]
        results.append({
            "sessionId": sid,
            "title": entry["title"],
            "folder": entry["folder"],
            "lastActivity": entry["lastActivity"],
            "live": sid in live_map,
            "status": live_map.get(sid, "archived"),
            "snippet": _snippet_around(text, idx, len(best_t)),
            "matches": int(matches),
            "_score": score,
        })
    results.sort(key=lambda r: (-r["_score"], -_epoch(r["lastActivity"])))
    for r in results:
        r.pop("_score", None)
    return results[:limit]


# --------------------------------------------------------------------------- #
# History / analytics (91-day heatmap, 30-day daily, hall of fame all-time)
# --------------------------------------------------------------------------- #

def _model_family(model):
    """Normalize a model id to a short family label for breakdowns."""
    m = (model or "").lower()
    if not m:
        return "unknown"
    if "opus" in m:
        return "Opus"
    if "sonnet" in m:
        return "Sonnet"
    if "haiku" in m:
        return "Haiku"
    if "fable" in m:
        return "Fable"
    return model


def compute_history():
    today = now_utc().date()
    start91 = today - timedelta(days=90)   # 91-day inclusive window
    start30 = today - timedelta(days=29)   # 30-day inclusive window

    day_heat = {}     # date -> prompts + replies
    day_daily = {}    # date -> {output,cost,prompts,tools} (last 30d only)
    byhour = [0] * 24
    bydow = [0] * 7   # Monday=0 .. Sunday=6
    tot_output = tot_prompts = tot_tools = 0
    tot_cost = 0.0
    active = set()
    hall = []
    # all-time breakdowns
    model_out, model_cost, model_sess = {}, {}, {}
    folder_cost, folder_out = {}, {}

    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    for path in files:
        agg = scan_file(path)
        if agg is None:
            continue
        tools_all = 0
        for dd in agg.get("per_day", {}).values():
            tools_all += dd.get("tools", 0)
        # all-time model + folder cost attribution (per file's totals)
        fam = _model_family(agg.get("model") or "")
        fout = int(agg.get("tok_output", 0))
        fcost = agg.get("cost", 0.0)
        model_out[fam] = model_out.get(fam, 0) + fout
        model_cost[fam] = model_cost.get(fam, 0.0) + fcost
        model_sess[fam] = model_sess.get(fam, 0) + 1
        fol = agg.get("folder") or ""
        folder_cost[fol] = folder_cost.get(fol, 0.0) + fcost
        folder_out[fol] = folder_out.get(fol, 0) + fout
        hall.append({
            "sessionId": _session_id_from_path(path),
            "title": agg.get("ai_title") or "Untitled session",
            "folder": agg.get("folder") or "",
            "output": int(agg.get("tok_output", 0)),
            "estCostUSD": round(agg.get("cost", 0.0), 4),
            "tools": int(tools_all),
        })
        for diso, dd in agg.get("per_day", {}).items():
            try:
                d = date.fromisoformat(diso)
            except Exception:
                continue
            if d < start91 or d > today:
                continue
            p = dd.get("prompts", 0)
            r = dd.get("replies", 0)
            t = dd.get("tools", 0)
            o = dd.get("output", 0)
            c = dd.get("cost", 0.0)
            day_heat[d] = day_heat.get(d, 0) + p + r
            tot_output += o
            tot_prompts += p
            tot_tools += t
            tot_cost += c
            if p or r or t:
                active.add(d)
            dow = d.weekday()
            for h, hc in dd.get("hours", {}).items():
                if 0 <= h < 24:
                    byhour[h] += hc
                    bydow[dow] += hc
            if d >= start30:
                e = day_daily.setdefault(
                    d, {"output": 0, "cost": 0.0, "prompts": 0, "tools": 0})
                e["output"] += o
                e["cost"] += c
                e["prompts"] += p
                e["tools"] += t

    heat_counts = [day_heat.get(start91 + timedelta(days=i), 0) for i in range(91)]
    mx = max(heat_counts) if heat_counts else 0
    heatmap = []
    for i in range(91):
        d = start91 + timedelta(days=i)
        c = heat_counts[i]
        if c <= 0 or mx <= 0:
            level = 0
        else:
            frac = c / mx
            level = 4 if frac >= 0.75 else 3 if frac >= 0.5 else 2 if frac >= 0.25 else 1
        heatmap.append({"date": d.isoformat(), "count": int(c), "level": level})

    daily = []
    for i in range(30):
        d = start30 + timedelta(days=i)
        e = day_daily.get(d)
        if e:
            daily.append({
                "date": d.isoformat(), "output": int(e["output"]),
                "cost": round(e["cost"], 4), "prompts": int(e["prompts"]),
                "tools": int(e["tools"]),
            })
        else:
            daily.append({
                "date": d.isoformat(), "output": 0, "cost": 0.0,
                "prompts": 0, "tools": 0,
            })

    hall.sort(key=lambda x: -x["output"])

    model_breakdown = sorted(
        ({"model": m, "output": int(model_out[m]),
          "estCostUSD": round(model_cost[m], 2), "sessions": int(model_sess[m])}
         for m in model_out),
        key=lambda x: -x["estCostUSD"])
    cost_by_folder = sorted(
        ({"folder": f, "estCostUSD": round(folder_cost[f], 2),
          "output": int(folder_out[f])}
         for f in folder_cost),
        key=lambda x: -x["estCostUSD"])[:8]

    return {
        "heatmap": heatmap,
        "daily": daily,
        "byHour": byhour,
        "byDow": bydow,
        "totals": {
            "transcripts": len(files),
            "activeDays": len(active),
            "output": int(tot_output),
            "estCostUSD": round(tot_cost, 2),
            "prompts": int(tot_prompts),
            "tools": int(tot_tools),
        },
        "hallOfFame": hall[:10],
        "modelBreakdown": model_breakdown,
        "costByFolder": cost_by_folder,
    }


def compute_pokedex():
    """Collection view across ALL transcripts. Each transcript maps to a species
    by the SAME hash(sessionId) % 48 used by creature_for(). Backed by scan_file's
    (mtime,size) cache so it stays responsive."""
    species = []
    for i in range(48):
        species.append({
            "species": i,
            "name": SPECIES_NAMES[i],
            "type": SPECIES_TYPES[i],
            "seed": int(species_seed(i)),
            "typeHue": int(TYPE_HUES.get(SPECIES_TYPES[i], 45)),
            "caught": False,
            "count": 0,
            "maxStage": 0,
            "totalOutput": 0,
            "shiny": False,
            "exampleSessionId": None,
            "_bestOut": -1,
        })

    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        sid = _session_id_from_path(path)
        agg = scan_file(path)
        if agg is None:
            continue
        h = _session_hash(sid)
        sp = h % 48
        shiny = shiny_for_species(sp)   # per-species (matches creature_for)
        stage, _, _ = stage_for(agg.get("prompt_count", 0))
        out = int(agg.get("tok_output", 0))
        d = species[sp]
        d["count"] += 1
        d["totalOutput"] += out
        if stage > d["maxStage"]:
            d["maxStage"] = stage
        if shiny:
            d["shiny"] = True
        # keep the highest-output session as the representative example
        if out > d["_bestOut"] or d["exampleSessionId"] is None:
            d["_bestOut"] = out
            d["exampleSessionId"] = sid

    caught_count = 0
    shiny_count = 0
    for d in species:
        d["caught"] = d["count"] > 0
        if d["caught"]:
            caught_count += 1
        if d["shiny"]:
            shiny_count += 1
        d.pop("_bestOut", None)

    return {
        "caughtCount": int(caught_count),
        "total": 48,
        "shinyCount": int(shiny_count),
        "species": species,
    }


def _known_folders():
    """The real set of project-dir basenames that actually contain transcripts.
    Used to validate a ?folder= slug (also prevents traversal: we only ever
    match against known dirs, never build a path from the raw slug)."""
    out = set()
    for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        out.add(os.path.basename(os.path.dirname(p)))
    return out


def compute_project(slug):
    """Per-project rollup across every transcript whose folder == slug.
    Returns None if the slug is not a known folder (caller -> 404).
    Reuses scan_file's (mtime,size) cache; never raises for bad data."""
    if not slug or slug not in _known_folders():
        return None

    today = now_utc().date()
    start91 = today - timedelta(days=90)   # 91-day inclusive window

    day_heat = {}          # date -> prompts + replies (THIS folder)
    active = set()         # distinct active dates (all-time)
    files_agg = {}         # path -> {path, action, count}
    model_out, model_cost, model_sess = {}, {}, {}
    sessions = []
    tot_prompts = tot_tools = tot_output = 0
    tot_cost = 0.0
    n_sessions = 0

    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        if os.path.basename(os.path.dirname(path)) != slug:
            continue
        agg = scan_file(path)
        if agg is None:
            continue
        n_sessions += 1

        # per-session tool total (sum across days)
        sess_tools = 0
        for dd in agg.get("per_day", {}).values():
            sess_tools += int(dd.get("tools", 0))

        # all-time totals + heatmap window
        for diso, dd in agg.get("per_day", {}).items():
            p = int(dd.get("prompts", 0))
            r = int(dd.get("replies", 0))
            t = int(dd.get("tools", 0))
            o = int(dd.get("output", 0))
            c = float(dd.get("cost", 0.0))
            tot_prompts += p
            tot_tools += t
            tot_output += o
            tot_cost += c
            try:
                d = date.fromisoformat(diso)
            except Exception:
                continue
            if p or r or t:
                active.add(d)
            if start91 <= d <= today:
                day_heat[d] = day_heat.get(d, 0) + p + r

        # files touched across this session
        for fe in (agg.get("files") or {}).values():
            key = fe.get("path")
            if not key:
                continue
            cur = files_agg.get(key)
            if cur is None:
                files_agg[key] = {
                    "path": key,
                    "action": fe.get("action") or "other",
                    "count": int(fe.get("count", 0)),
                }
            else:
                cur["count"] += int(fe.get("count", 0))

        # model attribution (per-file family)
        fam = _model_family(agg.get("model") or "")
        model_out[fam] = model_out.get(fam, 0) + int(agg.get("tok_output", 0))
        model_cost[fam] = model_cost.get(fam, 0.0) + float(agg.get("cost", 0.0))
        model_sess[fam] = model_sess.get(fam, 0) + 1

        la = agg.get("last_activity")
        sessions.append({
            "sessionId": _session_id_from_path(path),
            "title": agg.get("ai_title") or "Untitled session",
            "output": int(agg.get("tok_output", 0)),
            "estCostUSD": round(float(agg.get("cost", 0.0)), 4),
            "tools": int(sess_tools),
            "lastActivity": la.isoformat() if la else "",
        })

    # 91-day heatmap (oldest -> newest), levelled against the folder's own max
    heat_counts = [day_heat.get(start91 + timedelta(days=i), 0) for i in range(91)]
    mx = max(heat_counts) if heat_counts else 0
    heatmap = []
    for i in range(91):
        d = start91 + timedelta(days=i)
        c = heat_counts[i]
        if c <= 0 or mx <= 0:
            level = 0
        else:
            frac = c / mx
            level = 4 if frac >= 0.75 else 3 if frac >= 0.5 else 2 if frac >= 0.25 else 1
        heatmap.append({"date": d.isoformat(), "count": int(c), "level": level})

    top_files = sorted(files_agg.values(), key=lambda x: -x["count"])[:15]

    models = sorted(
        ({"model": m, "output": int(model_out[m]),
          "estCostUSD": round(model_cost[m], 4), "sessions": int(model_sess[m])}
         for m in model_out),
        key=lambda x: -x["estCostUSD"])

    sessions.sort(key=lambda s: -_epoch(s.get("lastActivity")))
    sessions = sessions[:40]

    return {
        "folder": slug,
        "prettyFolder": _pretty_folder(slug),
        "totals": {
            "sessions": int(n_sessions),
            "prompts": int(tot_prompts),
            "tools": int(tot_tools),
            "output": int(tot_output),
            "estCostUSD": round(tot_cost, 4),
            "activeDays": int(len(active)),
        },
        "heatmap": heatmap,
        "topFiles": top_files,
        "models": models,
        "sessions": sessions,
    }


def build_export_csv(payload):
    """CSV: one header + one row per LIVE session."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "title", "folder", "status", "promptCount", "ageSecs",
        "outputTokens", "estCostUSD", "model", "lastActivity",
    ])
    for s in payload.get("sessions", []):
        tok = s.get("tokens", {}) or {}
        w.writerow([
            s.get("sessionId", ""),
            s.get("title", ""),
            s.get("folder", ""),
            s.get("status", ""),
            s.get("promptCount", 0),
            s.get("ageSecs", 0),
            tok.get("output", 0),
            tok.get("estCostUSD", 0.0),
            tok.get("model", "") or "",
            s.get("lastActivity", ""),
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Daily digest — "what I did across all Claude sessions on <date>"
# --------------------------------------------------------------------------- #

def _pretty_folder(slug):
    """Turn a project-dir slug into a readable label (lossy, best-effort)."""
    if not slug:
        return "~"
    m = re.sub(r"^-Users-[^-]+-?", "", str(slug))
    return m if m else "~ (home)"


def _digest_day_detail(path, diso):
    """Back-compat single-day wrapper around _digest_range_detail."""
    return _digest_range_detail(path, {diso})


def _digest_range_detail(path, date_set):
    """Re-read one transcript, extracting the first human prompt, last assistant
    reply, and files touched across the given set of local-day iso strings. Cheap:
    only called for the handful of sessions active in the target range. When
    date_set has a single date this is identical to the old per-day behaviour."""
    first_prompt = None
    last_reply = None
    files = []
    seen_files = set()
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return first_prompt, last_reply, files
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            try:
                ts = parse_ts(o.get("timestamp"))
                if ts is None or ts.date().isoformat() not in date_set:
                    continue
                typ = o.get("type")
                if typ == "user":
                    content = (o.get("message") or {}).get("content")
                    if is_real_human_prompt(content) and first_prompt is None:
                        first_prompt = clean_prompt(content)
                elif typ == "assistant":
                    blocks = (o.get("message") or {}).get("content")
                    if isinstance(blocks, list):
                        texts = []
                        for b in blocks:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "text":
                                texts.append(b.get("text") or "")
                            elif b.get("type") == "tool_use":
                                inp = b.get("input") or {}
                                p = (inp.get("file_path") or inp.get("path")
                                     if isinstance(inp, dict) else None)
                                if isinstance(p, str) and p.strip():
                                    bn = os.path.basename(p.rstrip("/"))
                                    if bn and bn not in seen_files:
                                        seen_files.add(bn)
                                        files.append(bn)
                        if texts:
                            last_reply = strip_markdown("\n".join(texts))
            except Exception:
                continue
    return first_prompt, last_reply, files


def compute_digest(diso, days=1):
    """Build the digest payload (markdown + structured). With days=1 this is the
    single-day daily digest (unchanged). With days>1 it aggregates the last <days>
    days ENDING at <diso>, summing per-session stats across the range and including
    only sessions active somewhere in the range."""
    try:
        days = int(days)
    except Exception:
        days = 1
    days = max(1, min(31, days))

    try:
        end_d = date.fromisoformat(diso)
    except Exception:
        end_d = now_utc().astimezone().date()
        diso = end_d.isoformat()
    start_d = end_d - timedelta(days=days - 1)
    date_set = {(start_d + timedelta(days=i)).isoformat() for i in range(days)}

    entries = []
    tot = {"prompts": 0, "tools": 0, "output": 0, "cost": 0.0}
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        agg = scan_file(path)
        if agg is None:
            continue
        per_day = agg.get("per_day") or {}
        s_prompts = s_tools = s_output = 0
        s_cost = 0.0
        present = False
        for di in date_set:
            day = per_day.get(di)
            if not day:
                continue
            present = True
            s_prompts += day.get("prompts", 0)
            s_tools += day.get("tools", 0)
            s_output += day.get("output", 0)
            s_cost += day.get("cost", 0.0)
        if not present:
            continue
        fp, lr, files = _digest_range_detail(path, date_set)
        entries.append({
            "sessionId": _session_id_from_path(path),
            "title": agg.get("ai_title") or "Untitled session",
            "folder": _pretty_folder(agg.get("folder")),
            "prompts": s_prompts,
            "tools": s_tools,
            "output": s_output,
            "cost": round(s_cost, 4),
            "firstPrompt": fp or agg.get("first_prompt") or "",
            "lastReply": lr or agg.get("last_reply") or "",
            "files": files[:8],
            "links": [l.get("url") for l in (agg.get("links") or [])][:4],
        })
        tot["prompts"] += s_prompts
        tot["tools"] += s_tools
        tot["output"] += s_output
        tot["cost"] += s_cost

    entries.sort(key=lambda e: (-(e["prompts"] + e["tools"]), -e["output"]))

    if days == 1:
        lines = ["# Claude HQ — Daily Digest — %s" % diso, ""]
        empty_span = diso
    else:
        lines = ["# Claude HQ — Digest — %s → %s"
                 % (start_d.isoformat(), diso), ""]
        empty_span = "%s → %s" % (start_d.isoformat(), diso)
    if not entries:
        lines.append("_No Claude activity on %s._" % empty_span)
    else:
        lines.append(
            "**%d session%s active** · %d prompts · %d tool calls · ~%s output tokens · ~$%.2f list-price est."
            % (len(entries), "" if len(entries) == 1 else "s",
               tot["prompts"], tot["tools"], f"{tot['output']:,}", tot["cost"]))
        lines.append("")
        for e in entries:
            lines.append("## %s  (%s)" % (e["title"], e["folder"]))
            if e["firstPrompt"]:
                lines.append("- Started with: %s" % truncate(e["firstPrompt"], 220))
            if e["lastReply"]:
                lines.append("- Last outcome: %s" % truncate(e["lastReply"], 220))
            lines.append("- %d prompts · %d tools · ~%s output tok · ~$%.2f"
                         % (e["prompts"], e["tools"], f"{e['output']:,}", e["cost"]))
            if e["files"]:
                lines.append("- Files touched: %s" % ", ".join(e["files"]))
            if e["links"]:
                lines.append("- Links: %s" % " ".join(e["links"]))
            lines.append("")
    return {
        "date": diso,
        "days": days,
        "markdown": "\n".join(lines),
        "sessionCount": len(entries),
        "totals": {
            "prompts": tot["prompts"], "tools": tot["tools"],
            "output": tot["output"], "estCostUSD": round(tot["cost"], 2),
        },
    }


def compute_insights():
    """A handful of genuinely useful observations computed from the scan_file
    cache over ALL transcripts + the live payload. Each insight is guarded so it
    only appears when it has data. Never raises (falls back to empty list)."""
    try:
        today = now_utc().date()
        d7_start = today - timedelta(days=6)     # this week: [today-6 .. today]
        d14_start = today - timedelta(days=13)   # last week: [today-13 .. today-7]
        last_week_end = today - timedelta(days=7)
        d30_start = today - timedelta(days=29)

        day_cost = {}       # date -> est USD
        day_activity = {}   # date -> prompts + replies
        day_output = {}     # date -> output tokens
        folder_last = {}    # folder -> most recent activity date
        folder_week = {}    # folder -> prompts + tools in last 7d
        hourly = [0] * 24   # local-hour activity over 30d

        for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
            agg = scan_file(path)
            if agg is None:
                continue
            fol = agg.get("folder") or ""
            la = agg.get("last_activity")
            if la is not None:
                ld = la.date()
                if fol not in folder_last or ld > folder_last[fol]:
                    folder_last[fol] = ld
            for diso, dd in (agg.get("per_day") or {}).items():
                try:
                    d = date.fromisoformat(diso)
                except Exception:
                    continue
                if d > today:
                    continue
                p = dd.get("prompts", 0)
                t = dd.get("tools", 0)
                r = dd.get("replies", 0)
                day_cost[d] = day_cost.get(d, 0.0) + dd.get("cost", 0.0)
                day_activity[d] = day_activity.get(d, 0) + p + r
                day_output[d] = day_output.get(d, 0) + dd.get("output", 0)
                if d7_start <= d <= today:
                    folder_week[fol] = folder_week.get(fol, 0) + p + t
                if d30_start <= d <= today:
                    for h, hc in (dd.get("hours") or {}).items():
                        if 0 <= h < 24:
                            hourly[h] += hc

        insights = []

        # --- spend: this week vs last week ---
        this_week = sum(v for d, v in day_cost.items() if d7_start <= d <= today)
        last_week = sum(v for d, v in day_cost.items()
                        if d14_start <= d <= last_week_end)
        if this_week > 0 or last_week > 0:
            if last_week > 0:
                pct = (this_week - last_week) / last_week * 100.0
                arrow = "▲" if pct >= 0 else "▼"
                detail = ("Spend $%.2f this week (%s %d%% vs last week's $%.2f)"
                          % (this_week, arrow, abs(int(round(pct))), last_week))
                kind = "warn" if pct > 25 else "info"
            else:
                detail = "Spend $%.2f this week (nothing last week)" % this_week
                kind = "info"
            insights.append({"icon": "💸", "title": "Weekly spend",
                             "detail": detail, "kind": kind})

        # --- busiest project this week ---
        if folder_week:
            top_fol = max(folder_week, key=lambda k: folder_week[k])
            score = folder_week[top_fol]
            if score > 0:
                insights.append({
                    "icon": "🔥", "title": "Busiest project",
                    "detail": "%s — %d prompts + tool calls in the last 7 days"
                    % (_pretty_folder(top_fol), score),
                    "kind": "good"})

        # --- dormant projects (untouched > 14 days), up to 2 ---
        dormant = []
        for fol, ld in folder_last.items():
            age = (today - ld).days
            if age > 14:
                dormant.append((age, fol))
        dormant.sort(reverse=True)
        for age, fol in dormant[:2]:
            insights.append({
                "icon": "💤", "title": "Dormant project",
                "detail": "%s untouched %d days" % (_pretty_folder(fol), age),
                "kind": "warn"})

        # --- biggest day in the last 30 ---
        biggest = None
        for d, v in day_activity.items():
            if d30_start <= d <= today and v > 0:
                if biggest is None or v > biggest[1]:
                    biggest = (d, v)
        if biggest:
            insights.append({
                "icon": "📈", "title": "Biggest day",
                "detail": "%s was your busiest — %d prompts + replies"
                % (biggest[0].isoformat(), biggest[1]),
                "kind": "info"})

        # --- output this week ---
        out_week = sum(v for d, v in day_output.items() if d7_start <= d <= today)
        if out_week > 0:
            insights.append({
                "icon": "✍️", "title": "Output this week",
                "detail": "~%s output tokens in the last 7 days" % f"{out_week:,}",
                "kind": "info"})

        # --- tabs needing attention (stuck or needs), from live payload ---
        try:
            payload = build_payload_memo()
            needs = sum(1 for s in payload.get("sessions", [])
                        if s.get("status") == "needs")
            stuck = sum(1 for s in payload.get("sessions", []) if s.get("stuck"))
        except Exception:
            needs = stuck = 0
        attn = needs + stuck
        if attn > 0:
            insights.append({
                "icon": "⚠️", "title": "Needs attention",
                "detail": "%d tab%s stuck or awaiting input right now"
                % (attn, "" if attn == 1 else "s"),
                "kind": "warn"})

        # --- peak local hour over 30d ---
        if any(hourly):
            peak = max(range(24), key=lambda h: hourly[h])
            if hourly[peak] > 0:
                ampm = "am" if peak < 12 else "pm"
                h12 = peak % 12 or 12
                insights.append({
                    "icon": "🕒", "title": "Peak hour",
                    "detail": "Most active around %d%s local — %d actions over 30 days"
                    % (h12, ampm, hourly[peak]),
                    "kind": "info"})

        return {"generated": now_utc().isoformat(), "insights": insights}
    except Exception:
        return {"generated": now_utc().isoformat(), "insights": []}


# --------------------------------------------------------------------------- #
# Session actions (resume / reveal / close) — reached only via guarded POST.
# --------------------------------------------------------------------------- #

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _cwd_for_session(sid):
    """Best-effort cwd for a session id, from the live agent list."""
    try:
        for s in build_payload_memo().get("sessions", []):
            if s.get("sessionId") == sid:
                return s.get("cwd") or ""
    except Exception:
        pass
    return ""


def _find_kitty():
    """Locate the kitty binary, or None."""
    import shutil
    for p in ("/Applications/kitty.app/Contents/MacOS/kitty",
              os.path.expanduser("~/.local/bin/kitty"),
              "/opt/homebrew/bin/kitty", "/usr/local/bin/kitty"):
        if os.path.exists(p):
            return p
    return shutil.which("kitty")


def action_resume(sid):
    """Open a new kitty window running `claude --resume <sid>` in the session's cwd
    (falls back to Terminal.app if kitty isn't installed)."""
    if not sid or not _UUID_RE.match(sid) or not find_transcript(sid):
        return 400, {"error": "invalid or unknown sessionId"}
    cwd = _cwd_for_session(sid)
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")

    # sid is a validated UUID (safe charset); cwd is passed as a list arg (no shell).
    kitty = _find_kitty()
    try:
        if kitty:
            # --single-instance opens a new OS window in the running kitty (or starts one);
            # trailing argv runs the command, then `exec zsh -l` keeps the window open.
            subprocess.Popen(
                [kitty, "--single-instance", "--directory", cwd,
                 "zsh", "-lc", "claude --resume %s; exec zsh -l" % sid],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # bring kitty to the front (best-effort)
            subprocess.Popen(["open", "-a", "kitty"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 200, {"ok": True, "action": "resume", "sessionId": sid, "terminal": "kitty"}
        if '"' in cwd:
            return 400, {"error": "unsupported cwd"}
        inner = "cd %s && claude --resume %s" % (shlex.quote(cwd), sid)
        script = 'tell application "Terminal" to do script "%s"' % inner.replace("\\", "\\\\")
        subprocess.run(["osascript", "-e", script,
                        "-e", 'tell application "Terminal" to activate'],
                       check=False, timeout=15, capture_output=True, text=True)
        return 200, {"ok": True, "action": "resume", "sessionId": sid, "terminal": "Terminal"}
    except Exception as e:
        return 500, {"error": "resume failed: %s" % e}


def action_reveal(sid):
    """Open the session's working directory in Finder."""
    if not sid or not _UUID_RE.match(sid) or not find_transcript(sid):
        return 400, {"error": "invalid or unknown sessionId"}
    cwd = _cwd_for_session(sid)
    if not cwd or not os.path.isdir(cwd):
        return 400, {"error": "no folder for this session"}
    try:
        subprocess.run(["open", cwd], check=False, timeout=10,
                       capture_output=True, text=True)
    except Exception as e:
        return 500, {"error": "reveal failed: %s" % e}
    return 200, {"ok": True, "action": "reveal", "sessionId": sid}


def action_close(pid):
    """Terminate an interactive Claude session by pid (only if it IS claude)."""
    try:
        pid = int(pid)
    except Exception:
        return 400, {"error": "pid must be an integer"}
    if pid <= 1:
        return 400, {"error": "refusing that pid"}
    try:
        ps = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, timeout=10)
    except Exception as e:
        return 500, {"error": "ps failed: %s" % e}
    cmd = (ps.stdout or "").strip().lower()
    if not cmd or "claude" not in cmd:
        return 400, {"error": "pid %d is not a Claude process" % pid}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 400, {"error": "no such process"}
    except Exception as e:
        return 500, {"error": "close failed: %s" % e}
    return 200, {"ok": True, "action": "close", "pid": pid}


# --------------------------------------------------------------------------- #
# launchd auto-start (CLI only — never reachable over HTTP)
# --------------------------------------------------------------------------- #

def render_plist(port=None):
    port = port if port is not None else SERVER_PORT
    py = sys.executable or "python3"
    script = os.path.abspath(__file__)
    log = os.path.join(HERE, "claude-hq.log")
    err = os.path.join(HERE, "claude-hq.err.log")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '  <key>Label</key>\n  <string>%s</string>\n'
        '  <key>ProgramArguments</key>\n  <array>\n'
        '    <string>%s</string>\n    <string>%s</string>\n'
        '    <string>--port</string>\n    <string>%d</string>\n'
        '    <string>--no-open</string>\n  </array>\n'
        '  <key>EnvironmentVariables</key>\n  <dict>\n'
        '    <key>PATH</key>\n    <string>%s</string>\n  </dict>\n'
        '  <key>RunAtLoad</key>\n  <true/>\n'
        '  <key>KeepAlive</key>\n  <true/>\n'
        '  <key>StandardOutPath</key>\n  <string>%s</string>\n'
        '  <key>StandardErrorPath</key>\n  <string>%s</string>\n'
        '</dict>\n</plist>\n'
        % (LAUNCH_LABEL, py, script, port,
           "%s:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
           % os.path.expanduser("~/.local/bin"), log, err)
    )


def install_launchagent(port):
    os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
    with open(LAUNCH_PLIST, "w", encoding="utf-8") as f:
        f.write(render_plist(port))
    subprocess.run(["launchctl", "unload", LAUNCH_PLIST],
                   capture_output=True, text=True)
    r = subprocess.run(["launchctl", "load", LAUNCH_PLIST],
                       capture_output=True, text=True)
    print("Installed LaunchAgent: %s" % LAUNCH_PLIST)
    print("Claude HQ will now start at login on http://127.0.0.1:%d" % port)
    if r.returncode != 0 and r.stderr.strip():
        print("launchctl load said: %s" % r.stderr.strip())


def uninstall_launchagent():
    if os.path.exists(LAUNCH_PLIST):
        subprocess.run(["launchctl", "unload", LAUNCH_PLIST],
                       capture_output=True, text=True)
        os.remove(LAUNCH_PLIST)
        print("Removed LaunchAgent: %s" % LAUNCH_PLIST)
    else:
        print("No LaunchAgent installed (%s not found)." % LAUNCH_PLIST)


# --------------------------------------------------------------------------- #
# Server-persisted config + per-session meta (local JSON files in HERE).
# --------------------------------------------------------------------------- #

CONFIG_PATH = os.path.join(HERE, "config.json")
META_PATH = os.path.join(HERE, "sessions-meta.json")

KNOWN_THEMES = ("aurora", "midnight", "forest", "mono")
KNOWN_CREATURE_PACKS = ("monsters", "pokemon", "animals", "faces")
DEFAULT_CONFIG = {
    "theme": "aurora",
    "creaturePack": "monsters",
    "refreshMs": 5000,
    "stuckMinutes": 15,
    "dailyBudgetUSD": 0,
    "trainerName": "",
    "arenaUrl": "",
    "arenaEnabled": False,
    "arenaShareCost": False,
}

_config_lock = threading.Lock()
_meta_lock = threading.Lock()


def _validate_config(raw, base=None):
    """Return a clean config dict: only known keys, validated types/ranges.
    Invalid/unknown values fall back to `base` (defaults, or current config)."""
    cfg = dict(base if base is not None else DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    if raw.get("theme") in KNOWN_THEMES:
        cfg["theme"] = raw["theme"]
    if raw.get("creaturePack") in KNOWN_CREATURE_PACKS:
        cfg["creaturePack"] = raw["creaturePack"]
    try:
        rm = int(raw.get("refreshMs"))
        if 1000 <= rm <= 60000:
            cfg["refreshMs"] = rm
    except Exception:
        pass
    try:
        sm = int(raw.get("stuckMinutes"))
        if 1 <= sm <= 240:
            cfg["stuckMinutes"] = sm
    except Exception:
        pass
    try:
        db = float(raw.get("dailyBudgetUSD"))
        if db >= 0:
            cfg["dailyBudgetUSD"] = db
    except Exception:
        pass
    tn = raw.get("trainerName")
    if isinstance(tn, str):
        # printable chars only, whitespace collapsed, capped; "" = auto-derive
        tn = "".join(ch for ch in tn if ch.isprintable())
        cfg["trainerName"] = " ".join(tn.split())[:32]
    au = raw.get("arenaUrl")
    if isinstance(au, str):
        au = au.strip()
        # http/https only: this string becomes an outbound request target.
        cfg["arenaUrl"] = au[:256] if au.startswith(("http://", "https://")) else ""
    for key in ("arenaEnabled", "arenaShareCost"):
        if key in raw:
            cfg[key] = bool(raw.get(key))
    return cfg


def load_config():
    """Read + validate config.json; returns defaults if missing/corrupt."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    return _validate_config(raw)


def save_config(patch):
    """Merge `patch` into the current config, validate, persist, return saved."""
    with _config_lock:
        cur = load_config()
        cfg = _validate_config(patch, base=cur)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass
        return cfg


def _clean_meta_entry(entry):
    """Normalise one session-meta entry: pinned bool, tags list, note capped."""
    if not isinstance(entry, dict):
        entry = {}
    pinned = bool(entry.get("pinned"))
    tags = []
    raw_tags = entry.get("tags")
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if not isinstance(t, str):
                continue
            t = t.strip()[:24]
            if t:
                tags.append(t)
            if len(tags) >= 12:
                break
    note = entry.get("note")
    note = note[:2000] if isinstance(note, str) else ""
    name = entry.get("name")
    name = name.strip()[:80] if isinstance(name, str) else ""
    return {"pinned": pinned, "tags": tags, "note": note, "name": name}


def load_meta():
    """Read sessions-meta.json -> {sessionId: {pinned,tags,note}} (validated)."""
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    out = {}
    if isinstance(raw, dict):
        for sid, entry in raw.items():
            if isinstance(sid, str) and _UUID_RE.match(sid):
                out[sid] = _clean_meta_entry(entry)
    return out


def save_meta(sid, patch):
    """Merge `patch` into the meta entry for `sid`, persist, return the entry."""
    with _meta_lock:
        data = load_meta()
        cur = data.get(sid, {"pinned": False, "tags": [], "note": "", "name": ""})
        merged = dict(cur)
        if isinstance(patch, dict):
            if "pinned" in patch:
                merged["pinned"] = patch["pinned"]
            if "tags" in patch:
                merged["tags"] = patch["tags"]
            if "note" in patch:
                merged["note"] = patch["note"]
            if "name" in patch:
                merged["name"] = patch["name"]
        entry = _clean_meta_entry(merged)
        data[sid] = entry
        try:
            with open(META_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return entry


# --------------------------------------------------------------------------- #
# Full-conversation transcript events (paged view + markdown export).
# Cached per (path, mtime, size), like scan_file / search entries.
# --------------------------------------------------------------------------- #

_transcript_cache = {}
_transcript_lock = threading.Lock()


def _iter_transcript_events(path):
    """Read one transcript into an ordered list of conversation events:
    {i, t(iso), role in you|claude|tool|system, text(<=1200), tool(str|None)}."""
    events = []
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return events
    i = 0
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            try:
                typ = o.get("type")
                ts = parse_ts(o.get("timestamp"))
                tiso = ts.isoformat() if ts else ""
                if typ == "user":
                    content = (o.get("message") or {}).get("content")
                    if is_real_human_prompt(content):
                        events.append({
                            "i": i, "t": tiso, "role": "you",
                            "text": truncate(clean_prompt(content), 1200),
                            "tool": None,
                        })
                        i += 1
                elif typ == "assistant":
                    blocks = (o.get("message") or {}).get("content")
                    if isinstance(blocks, list):
                        texts = []
                        for b in blocks:
                            if not isinstance(b, dict):
                                continue
                            bt = b.get("type")
                            if bt == "text":
                                txt = b.get("text") or ""
                                if txt.strip():
                                    texts.append(txt)
                            elif bt == "tool_use":
                                if texts:
                                    events.append({
                                        "i": i, "t": tiso, "role": "claude",
                                        "text": truncate(
                                            strip_markdown("\n".join(texts)), 1200),
                                        "tool": None,
                                    })
                                    i += 1
                                    texts = []
                                name = b.get("name") or "Tool"
                                events.append({
                                    "i": i, "t": tiso, "role": "tool",
                                    "text": truncate(tool_label(b) or name, 1200),
                                    "tool": name,
                                })
                                i += 1
                        if texts:
                            events.append({
                                "i": i, "t": tiso, "role": "claude",
                                "text": truncate(
                                    strip_markdown("\n".join(texts)), 1200),
                                "tool": None,
                            })
                            i += 1
                elif typ == "system":
                    content = o.get("content")
                    if isinstance(content, str) and _match_error(content):
                        events.append({
                            "i": i, "t": tiso, "role": "system",
                            "text": truncate(strip_markdown(content), 1200),
                            "tool": None,
                        })
                        i += 1
            except Exception:
                continue
    return events


def get_transcript_events(path):
    """Cached ordered events for one transcript, rebuilt on (mtime,size) change."""
    if not path:
        return []
    try:
        st = os.stat(path)
    except Exception:
        return []
    key = (st.st_mtime, st.st_size)
    with _transcript_lock:
        c = _transcript_cache.get(path)
        if c is not None and c.get("_key") == key:
            return c["events"]
    events = _iter_transcript_events(path)
    with _transcript_lock:
        _transcript_cache[path] = {"_key": key, "events": events}
    return events


def build_session_markdown(sid, path):
    """Render a whole session as a readable Markdown transcript."""
    agg = scan_file(path)
    events = get_transcript_events(path)
    title = (agg.get("ai_title") if isinstance(agg, dict) else None) \
        or "Untitled session"
    folder = _pretty_folder(agg.get("folder")) if isinstance(agg, dict) else "~"
    ts_list = [e["t"] for e in events if e.get("t")]
    lines = ["# %s" % title, "",
             "- Folder: %s" % folder,
             "- Session: %s" % sid]
    if ts_list:
        lines.append("- Range: %s → %s"
                     % (ts_list[0][:19].replace("T", " "),
                        ts_list[-1][:19].replace("T", " ")))
    lines.append("")
    for e in events:
        role = e.get("role")
        text = e.get("text") or ""
        if role == "you":
            lines.append("**You:** %s" % text)
        elif role == "claude":
            lines.append("**Claude:** %s" % text)
        elif role == "tool":
            lines.append("`%s` %s" % (e.get("tool") or "tool", text))
        elif role == "system":
            lines.append("_system:_ %s" % text)
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeHQ/" + APP_VERSION

    def _host_ok(self):
        host = self.headers.get("Host", "")
        hostname = host.split(":")[0].strip().lower()
        return hostname in ("127.0.0.1", "localhost", "")

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_download(self, code, body, content_type, filename):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % filename)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_HEAD(self):
        """Header-only responses (so `curl -I` works for exports); never a body."""
        if not self._host_ok():
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        if path == "/api/export.csv":
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="claude-hq-sessions.csv"')
        elif path == "/api/export.json":
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="claude-hq-export.json"')
        elif path == "/":
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            self._send(403, "Forbidden: local access only\n", "text/plain; charset=utf-8")
            return

        path = self.path.split("?", 1)[0]

        if path == "/":
            try:
                with open(INDEX_HTML, "r", encoding="utf-8") as f:
                    html = f.read()
                # Inject the per-process CSRF token (same-origin can read it only).
                html = html.replace(CSRF_PLACEHOLDER, CSRF_TOKEN)
                self._send(200, html, "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(
                    200,
                    "<!doctype html><meta charset=utf-8><title>Claude Dashboard</title>"
                    "<body style='font-family:system-ui;padding:2rem'>"
                    "<h1>Claude Dashboard</h1><p>index.html not found next to dashboard.py. "
                    "API is live at <a href='/api/sessions'>/api/sessions</a>.</p>",
                    "text/html; charset=utf-8",
                )
            except Exception as e:
                self._send(500, f"error reading index.html: {e}\n", "text/plain; charset=utf-8")
            return

        if path == "/manifest.webmanifest":
            self._send(200, json.dumps({
                "name": "Claude HQ", "short_name": "Claude HQ",
                "start_url": "/", "scope": "/", "display": "standalone",
                "background_color": "#0e0f1a", "theme_color": "#6c5cff",
                "description": "Local dashboard for your Claude Code sessions.",
                "icons": [{"src": "/icon.svg", "sizes": "any",
                           "type": "image/svg+xml", "purpose": "any maskable"}],
            }), "application/manifest+json; charset=utf-8")
            return

        if path == "/icon.svg":
            self._send(200,
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
                '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
                '<stop offset="0" stop-color="#6c5cff"/><stop offset="1" stop-color="#9b8bff"/>'
                '</linearGradient></defs>'
                '<rect width="512" height="512" rx="112" fill="url(#g)"/>'
                '<path d="M286 64 176 288h74l-40 160 150-240h-84z" fill="#fff"/></svg>',
                "image/svg+xml; charset=utf-8")
            return

        if path == "/sw.js":
            # network-first SW: never serves stale content, but enables install +
            # an offline fallback to the last cached shell.
            self._send(200,
                'const C="claude-hq-v1";'
                'self.addEventListener("install",e=>self.skipWaiting());'
                'self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));'
                'self.addEventListener("fetch",e=>{'
                'const u=new URL(e.request.url);'
                'if(e.request.method!=="GET"||u.pathname.startsWith("/api/"))return;'
                'e.respondWith(fetch(e.request).then(r=>{'
                'if(u.pathname==="/"){const c=r.clone();caches.open(C).then(x=>x.put("/",c));}'
                'return r;}).catch(()=>caches.match(u.pathname==="/"?"/":e.request)));'
                '});',
                "application/javascript; charset=utf-8")
            return

        if path == "/api/sessions":
            try:
                payload = build_payload_memo()
            except Exception as e:
                payload = {
                    "updated": now_utc().isoformat(),
                    "season": _empty_season(),
                    "sessions": [],
                    "error": f"internal error: {e}",
                }
            self._send(200, json.dumps(payload))
            return

        if path == "/api/arena/status":
            self._send(200, json.dumps(arena.status()))
            return

        if path == "/api/arena/preview":
            code, resp = arena.preview(PROJECTS_DIR)
            self._send(code or 200, json.dumps(resp))
            return

        if path == "/api/arena/board":
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                       if "?" in self.path else "")
            window = (qs.get("window", ["season"])[0] or "season")
            if window not in ("season", "30d", "7d", "all"):
                window = "season"
            code, resp = arena.board(window)
            self._send(code or 502, json.dumps(resp))
            return

        if path == "/api/arena/raid":
            code, resp = arena.raid()
            self._send(code or 502, json.dumps(resp))
            return

        if path == "/api/config":
            try:
                self._send(200, json.dumps(load_config()))
            except Exception:
                self._send(200, json.dumps(dict(DEFAULT_CONFIG)))
            return

        if path == "/api/meta":
            try:
                self._send(200, json.dumps(load_meta()))
            except Exception:
                self._send(200, json.dumps({}))
            return

        if path.startswith("/api/transcript/"):
            sid = path[len("/api/transcript/"):]
            if not sid or "/" in sid or ".." in sid or "\\" in sid:
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            tpath = find_transcript(sid)
            if not tpath:
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            try:
                import urllib.parse
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                           if "?" in self.path else "")
                try:
                    offset = int(qs.get("offset", ["0"])[0])
                except Exception:
                    offset = 0
                try:
                    limit = int(qs.get("limit", ["60"])[0])
                except Exception:
                    limit = 60
                if offset < 0:
                    offset = 0
                limit = max(1, min(200, limit))
                q = (qs.get("q", [""])[0] or "").strip().lower()
                events = get_transcript_events(tpath)
                agg = scan_file(tpath)
                title = (agg.get("ai_title") if isinstance(agg, dict) else None) \
                    or "Untitled session"
                matched = None
                if q:
                    events = [e for e in events
                              if q in (e.get("text") or "").lower()
                              or q in (e.get("tool") or "").lower()]
                    matched = len(events)
                page = events[offset:offset + limit]
                self._send(200, json.dumps({
                    "sessionId": sid, "title": title, "total": len(events),
                    "matched": matched, "query": q,
                    "offset": offset, "limit": limit, "events": page,
                }))
            except Exception as e:
                self._send(404, json.dumps({"error": "unknown session: %s" % e}))
            return

        if path.startswith("/api/session/") and path.endswith("/export.md"):
            sid = path[len("/api/session/"):-len("/export.md")]
            if not sid or "/" in sid or ".." in sid or "\\" in sid:
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            tpath = find_transcript(sid)
            if not tpath:
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            try:
                md = build_session_markdown(sid, tpath)
            except Exception as e:
                self._send(404, json.dumps({"error": "unknown session: %s" % e}))
                return
            self._send_download(200, md, "text/markdown; charset=utf-8",
                                "claude-hq-session-%s.md"
                                % ((sid or "")[:8] or "session"))
            return

        if path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            # reject empty / path-traversal-ish ids outright
            if not sid or "/" in sid or ".." in sid or "\\" in sid:
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            try:
                detail = build_session_detail(sid)
            except Exception as e:
                self._send(404, json.dumps({"error": f"unknown session: {e}"}))
                return
            if detail is None:
                self._send(404, json.dumps({"error": "unknown session"}))
                return
            self._send(200, json.dumps(detail))
            return

        if path == "/api/stream":
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
            except Exception:
                return
            last = None
            try:
                while True:
                    try:
                        payload = build_payload_memo()
                        blob = json.dumps(payload)
                    except Exception as e:
                        blob = json.dumps({"error": str(e)})
                    if blob != last:
                        self.wfile.write(("data: " + blob + "\n\n").encode("utf-8"))
                        last = blob
                    else:
                        self.wfile.write(b":keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(1.5)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return
            return

        if path == "/api/search":
            try:
                import urllib.parse
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                           if "?" in self.path else "")
                q = (qs.get("q", [""])[0] or "").strip()
                if not q:
                    self._send(200, json.dumps({"query": qs.get("q", [""])[0],
                                                "count": 0, "results": []}))
                    return
                results = search_transcripts(q)
                self._send(200, json.dumps({
                    "query": q, "count": len(results), "results": results,
                }))
            except Exception as e:
                self._send(200, json.dumps({"query": "", "count": 0,
                                            "results": [], "error": str(e)}))
            return

        if path == "/api/project":
            try:
                import urllib.parse
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                           if "?" in self.path else "")
                slug = (qs.get("folder", [""])[0] or "").strip()
                data = compute_project(slug)
                if data is None:
                    self._send(404, json.dumps({"error": "unknown folder"}))
                    return
                self._send(200, json.dumps(data))
            except Exception as e:
                self._send(200, json.dumps({
                    "folder": "", "prettyFolder": "", "totals": {},
                    "heatmap": [], "topFiles": [], "models": [], "sessions": [],
                    "error": str(e),
                }))
            return

        if path == "/api/history":
            try:
                self._send(200, json.dumps(compute_history()))
            except Exception as e:
                self._send(200, json.dumps({
                    "heatmap": [], "daily": [], "byHour": [0] * 24,
                    "byDow": [0] * 7, "totals": {}, "hallOfFame": [],
                    "error": str(e),
                }))
            return

        if path == "/api/insights":
            try:
                self._send(200, json.dumps(compute_insights()))
            except Exception as e:
                self._send(200, json.dumps({
                    "generated": now_utc().isoformat(),
                    "insights": [], "error": str(e),
                }))
            return

        if path == "/api/pokedex":
            try:
                self._send(200, json.dumps(compute_pokedex()))
            except Exception as e:
                self._send(200, json.dumps({
                    "caughtCount": 0, "total": 48, "shinyCount": 0,
                    "species": [], "error": str(e),
                }))
            return

        if path == "/api/export.json":
            try:
                p = build_payload_memo()
                body = json.dumps({
                    "generated": now_utc().isoformat(),
                    "season": p.get("season"),
                    "sessions": p.get("sessions", []),
                })
            except Exception as e:
                body = json.dumps({"generated": now_utc().isoformat(),
                                   "error": str(e)})
            self._send_download(200, body, "application/json; charset=utf-8",
                                "claude-hq-export.json")
            return

        if path == "/api/export.csv":
            try:
                body = build_export_csv(build_payload_memo())
            except Exception as e:
                body = "error\n%s\n" % str(e).replace("\n", " ")
            self._send_download(200, body, "text/csv; charset=utf-8",
                                "claude-hq-sessions.csv")
            return

        if path == "/api/digest":
            try:
                import urllib.parse
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                           if "?" in self.path else "")
                diso = (qs.get("date", [""])[0] or "").strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", diso):
                    diso = now_utc().astimezone().date().isoformat()
                try:
                    days = int(qs.get("days", ["1"])[0])
                except Exception:
                    days = 1
                days = max(1, min(31, days))
                data = compute_digest(diso, days)
                if qs.get("download", ["0"])[0] in ("1", "true", "yes"):
                    self._send_download(200, data["markdown"],
                                        "text/markdown; charset=utf-8",
                                        "claude-hq-digest-%s.md" % diso)
                    return
                self._send(200, json.dumps(data))
            except Exception as e:
                self._send(200, json.dumps({
                    "date": "", "markdown": "# Digest error\n\n%s" % e,
                    "sessionCount": 0, "totals": {}, "error": str(e),
                }))
            return

        self._send(404, json.dumps({"error": "not found"}))

    # ----- state-changing actions (guarded) ------------------------------- #

    def _origin_ok(self):
        """Reject cross-site POSTs even if they carry a valid Host header."""
        origin = self.headers.get("Origin")
        if origin:
            try:
                import urllib.parse
                host = urllib.parse.urlparse(origin).hostname
            except Exception:
                return False
            if host not in ("127.0.0.1", "localhost"):
                return False
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs == "cross-site":
            return False
        return True

    def _arena_post(self, path, body):
        """Arena actions. The device token never crosses back to the page."""
        try:
            if path == "/api/arena/pair":
                code = body.get("code")
                if not isinstance(code, str) or not code.strip():
                    return 400, {"error": "pairing code required"}
                return arena.pair(code, label=body.get("label", ""))
            if path == "/api/arena/unpair":
                arena.clear_link()
                return 200, {"ok": True}
            if path == "/api/arena/publish":
                return arena.publish(PROJECTS_DIR)
            if path == "/api/arena/ticket":
                return arena.ws_ticket()
            if path == "/api/arena/nudge":
                to = body.get("toHandle")
                if not isinstance(to, str) or not to.strip():
                    return 400, {"error": "toHandle required"}
                return arena.send_nudge(to.strip(), note=body.get("note", ""))
        except Exception as e:
            return 500, {"error": "arena request failed: %s" % e}
        return 404, {"error": "not found"}

    def do_POST(self):
        if not self._host_ok():
            self._send(403, json.dumps({"error": "local access only"}))
            return
        # CSRF: exact token match required (same-origin page only can supply it).
        token = self.headers.get("X-HQ-Token", "")
        if not token or not secrets.compare_digest(token, CSRF_TOKEN):
            self._send(403, json.dumps({"error": "bad or missing CSRF token"}))
            return
        if not self._origin_ok():
            self._send(403, json.dumps({"error": "cross-site request rejected"}))
            return

        path = self.path.split("?", 1)[0]
        if path not in ("/api/action", "/api/config", "/api/meta",
                        "/api/arena/pair", "/api/arena/unpair",
                        "/api/arena/publish", "/api/arena/ticket",
                        "/api/arena/nudge"):
            self._send(404, json.dumps({"error": "not found"}))
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except Exception as e:
            self._send(400, json.dumps({"error": "bad JSON body: %s" % e}))
            return

        if path.startswith("/api/arena/"):
            code, resp = self._arena_post(path, body)
            self._send(code, json.dumps(resp))
            return

        if path == "/api/config":
            try:
                self._send(200, json.dumps(save_config(body)))
            except Exception as e:
                self._send(400, json.dumps({"error": "bad config: %s" % e}))
            return

        if path == "/api/meta":
            sid = body.get("sessionId")
            if not isinstance(sid, str) or not _UUID_RE.match(sid):
                self._send(400, json.dumps({"error": "invalid sessionId"}))
                return
            try:
                self._send(200, json.dumps(save_meta(sid, body)))
            except Exception as e:
                self._send(400, json.dumps({"error": "bad meta: %s" % e}))
            return

        action = body.get("action")
        try:
            if action == "resume":
                code, resp = action_resume(body.get("sessionId"))
            elif action == "reveal":
                code, resp = action_reveal(body.get("sessionId"))
            elif action == "close":
                code, resp = action_close(body.get("pid"))
            else:
                code, resp = 400, {"error": "unknown action"}
        except Exception as e:
            code, resp = 500, {"error": "action failed: %s" % e}
        self._send(code, json.dumps(resp))

    def log_message(self, fmt, *args):
        # keep the console quiet-ish
        return


def main():
    ap = argparse.ArgumentParser(description="Local Claude sessions dashboard.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--print-plist", action="store_true",
                    help="Print the launchd LaunchAgent plist and exit (no side effects).")
    ap.add_argument("--install", action="store_true",
                    help="Install a launchd LaunchAgent so Claude HQ starts at login.")
    ap.add_argument("--uninstall", action="store_true",
                    help="Remove the launchd LaunchAgent.")
    args = ap.parse_args()

    global SERVER_PORT
    SERVER_PORT = args.port

    if args.print_plist:
        sys.stdout.write(render_plist(args.port))
        return
    if args.uninstall:
        uninstall_launchagent()
        return
    if args.install:
        install_launchagent(args.port)
        return

    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    # Arena (multiplayer) stays dormant until the user pairs and enables it.
    arena.init(scan_file, load_config, HERE)
    arena.start_publisher(PROJECTS_DIR)

    # Raise a native macOS notification for an incoming nudge, so it reaches you
    # even with no Arena tab open (as long as this process is running).
    def _notify(title, body):
        try:
            subprocess.run(
                ["osascript", "-e",
                 'display notification %s with title %s sound name "Ping"'
                 % (json.dumps(body), json.dumps(title))],
                check=False, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    arena.start_nudge_poller(_notify)

    # Warm the per-file scan + search caches in the background so the first
    # /api/search and /api/history are instant instead of a one-time ~1s scan.
    def _prewarm():
        try:
            for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
                try:
                    scan_file(p)
                    get_search_entry(p)
                except Exception:
                    continue
        except Exception:
            pass
    threading.Thread(target=_prewarm, daemon=True).start()

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    print(f"Claude Dashboard serving at {url}  (127.0.0.1 only — private)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
