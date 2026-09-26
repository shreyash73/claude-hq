"""
Wire formats.

`StatPayload` is the privacy boundary. It uses `extra="forbid"`, so a client
that grows a new field cannot silently start leaking it — the server rejects the
whole submission until the field is added here deliberately. Nothing in this
module can carry prompt text, file paths, project names or session titles.
"""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1

# Tool names are echoed back on the board, so only known built-ins are accepted.
# Anything else -- notably `mcp__<server>__<tool>`, which can carry an employer's
# or client's name -- must be bucketed into "Other" by the client. The server
# enforces the same rule so a careless client cannot leak one.
KNOWN_TOOLS = frozenset({
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "NotebookEdit",
    "Glob", "Grep", "Task", "Agent", "WebFetch", "WebSearch", "TodoWrite",
    "ExitPlanMode", "EnterPlanMode", "SlashCommand", "Skill", "AskUserQuestion",
    "Artifact", "Workflow", "Monitor", "ToolSearch", "Other",
})

Handle = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class TokenCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = Field(0, ge=0)
    output: int = Field(0, ge=0)
    cacheRead: int = Field(0, ge=0)
    cacheCreation: int = Field(0, ge=0)


class ToolCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=48)
    count: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def _allowlisted(cls, v: str) -> str:
        return v if v in KNOWN_TOOLS else "Other"


class DayStat(BaseModel):
    """One day of raw activity. No score -- the server derives that."""

    model_config = ConfigDict(extra="forbid")

    date: date
    prompts: int = Field(0, ge=0)
    tools: int = Field(0, ge=0)
    artifacts: int = Field(0, ge=0)
    replies: int = Field(0, ge=0)
    tokens: TokenCounts = Field(default_factory=TokenCounts)
    toolBreakdown: list[ToolCount] = Field(default_factory=list, max_length=32)
    # Opt-in. Omitted entirely unless the user turned on cost sharing.
    costUSD: float | None = Field(None, ge=0)


class StatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1]
    trainerName: str = Field("", max_length=32)
    days: list[DayStat] = Field(max_length=400)

    @field_validator("trainerName")
    @classmethod
    def _clean(cls, v: str) -> str:
        return " ".join(v.split())[:32]


# --- responses -------------------------------------------------------------

class BoardEntry(BaseModel):
    rank: int
    handle: str
    displayName: str
    trainerName: str
    avatarUrl: str
    xp: int
    level: int
    rankTitle: str
    prompts: int
    tools: int
    artifacts: int
    activeDays: int
    streak: int
    tokensTotal: int
    costUSD: float | None = None
    isYou: bool = False


class BoardResponse(BaseModel):
    window: str
    startsOn: date
    endsOn: date
    seasonName: str
    generatedAt: str
    entries: list[BoardEntry]


class MeResponse(BaseModel):
    handle: str
    displayName: str
    trainerName: str
    avatarUrl: str
    deviceLabel: str


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    notes: list[str] = Field(default_factory=list)


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=4, max_length=32)
    label: str = Field("", max_length=64)


class PairResponse(BaseModel):
    token: str
    handle: str
    displayName: str
    avatarUrl: str


class TicketResponse(BaseModel):
    ticket: str
    expiresIn: int


# --- nudges ----------------------------------------------------------------

class SendNudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    toHandle: str = Field(min_length=1, max_length=64)
    note: str = Field("", max_length=120)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, v: str) -> str:
        return "".join(ch for ch in v if ch.isprintable()).strip()[:120]


class SendNudgeResponse(BaseModel):
    queued: bool
    deliveredLive: int = 0


class NudgeItem(BaseModel):
    fromHandle: str
    fromName: str
    note: str
    at: str


class NudgesResponse(BaseModel):
    nudges: list[NudgeItem] = Field(default_factory=list)


# --- co-op raids -------------------------------------------------------------
# Derived entirely from daily counts; nothing here is submitted by a client.

class RaidBoss(BaseModel):
    slug: str
    name: str
    emoji: str
    weakness: Literal["prompts", "tools", "artifacts"]
    flavor: str
    multiplier: int


class RaidRaider(BaseModel):
    handle: str
    displayName: str
    trainerName: str
    avatarUrl: str
    damage: int
    share: float
    todayDamage: int
    capped: bool = False
    mvp: bool = False
    isYou: bool = False


class RaidHistoryItem(BaseModel):
    weekStart: date
    bossName: str
    emoji: str
    hp: int
    damage: int
    defeated: bool
    raiders: int


class RaidResponse(BaseModel):
    weekStart: date
    weekEnd: date
    endsAt: str
    boss: RaidBoss
    hp: int
    damage: int
    defeated: bool
    defeatedOn: date | None = None
    raiderCap: int
    raiders: list[RaidRaider]
    history: list[RaidHistoryItem]
    yourTrophies: int
    teamStreak: int
    generatedAt: str
