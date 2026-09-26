"""
Co-op raids: a weekly boss the whole Arena fights together.

Everything here is *derived* from `daily_stats`, the same raw counts the board
reads. There is no raid table, no new wire field and no migration: the boss, its
HP and every hit are recomputed on each request. That keeps the privacy model
exactly where it was (counts only) and means tuning a boss is a code change,
not a data fix.

A raid runs one ISO week, Monday to Sunday UTC. Damage is the same XP formula as
the board, except the boss's weakness counts double. So that one heavy user
cannot solo it, each raider's damage is capped at a share of the boss's HP; the
cap loosens when fewer people are raiding.
"""
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DailyStat, User
from .schemas import (
    RaidBoss, RaidHistoryItem, RaidResponse, RaidRaider,
)
from .scoring import XP_PER_ARTIFACT, XP_PER_PROMPT, XP_PER_TOOL

# Floor on HP per raider, so a quiet week still has a boss worth fighting.
HP_PER_RAIDER = 2000
# The boss is this much tougher than the team's recent weekly average.
HP_STRETCH = 1.1
HP_ROUND_TO = 500
# How many prior weeks set the "recent average" and count someone as a raider.
LOOKBACK_WEEKS = 4
# No raider may deal more than this share of HP, unless the team is smaller
# than 1 / MAX_SHARE, in which case the cap is an even split.
MAX_SHARE = 0.4
WEAKNESS_MULTIPLIER = 2
HISTORY_WEEKS = 6


@dataclass(frozen=True)
class Boss:
    slug: str
    name: str
    emoji: str
    weakness: str  # "prompts" | "tools" | "artifacts"
    flavor: str


BOSSES = (
    Boss("bug-hydra", "Bug Hydra", "🐉", "tools",
         "Fix one bug and two grow back. Only relentless tool calls keep up."),
    Boss("token-leviathan", "Token Leviathan", "🐋", "artifacts",
         "It swallows context whole. Ship artifacts to choke it."),
    Boss("merge-golem", "Merge Golem", "🗿", "prompts",
         "Built from a thousand conflicts. Talk it down, prompt by prompt."),
    Boss("flaky-phantom", "Flaky Phantom", "👻", "tools",
         "Passes locally, fails in CI. Re-run everything."),
    Boss("scope-kraken", "Scope Kraken", "🦑", "artifacts",
         "Every tentacle is a new requirement. Deliver to cut them off."),
    Boss("legacy-lich", "Legacy Lich", "💀", "prompts",
         "Undocumented since 2009. Ask it questions until it crumbles."),
    Boss("yak-titan", "Yak Titan", "🦬", "tools",
         "You came to change one line. Now you are shaving this."),
    Boss("deadline-dragon", "Deadline Dragon", "🔥", "artifacts",
         "It lands on Friday at 5pm. Ship before it does."),
)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def boss_for_week(start: date) -> Boss:
    iso_year, iso_week, _ = start.isocalendar()
    return BOSSES[(iso_year * 53 + iso_week) % len(BOSSES)]


def damage(boss: Boss, prompts: int, tools: int, artifacts: int) -> int:
    mult = {"prompts": 1, "tools": 1, "artifacts": 1}
    mult[boss.weakness] = WEAKNESS_MULTIPLIER
    return (
        prompts * XP_PER_PROMPT * mult["prompts"]
        + tools * XP_PER_TOOL * mult["tools"]
        + artifacts * XP_PER_ARTIFACT * mult["artifacts"]
    )


def _raw_xp(prompts: int, tools: int, artifacts: int) -> int:
    return prompts * XP_PER_PROMPT + tools * XP_PER_TOOL + artifacts * XP_PER_ARTIFACT


def boss_hp(raiders: int, recent_weekly_xp: list[int]) -> int:
    """HP scales with the team: at least HP_PER_RAIDER each, and a stretch above
    whatever the team has actually been doing lately."""
    floor = max(1, raiders) * HP_PER_RAIDER
    avg = sum(recent_weekly_xp) / len(recent_weekly_xp) if recent_weekly_xp else 0
    hp = max(floor, avg * HP_STRETCH)
    return int(-(-hp // HP_ROUND_TO) * HP_ROUND_TO)  # round up


def share_cap(hp: int, raiders: int) -> int:
    return int(hp * max(MAX_SHARE, 1 / max(1, raiders)))


@dataclass
class _Week:
    start: date
    boss: Boss
    hp: int
    # user_id -> [(day, prompts, tools, artifacts)]
    days: dict[str, list[tuple[date, int, int, int]]]
    raider_count: int


def _settle(week: _Week) -> tuple[dict[str, int], int, date | None]:
    """Apply the per-raider cap day by day. Returns (capped damage per user,
    total, the day HP first hit zero)."""
    cap = share_cap(week.hp, week.raider_count)
    dealt: dict[str, int] = {}
    total = 0
    defeated_on = None
    for offset in range(7):
        d = week.start + timedelta(days=offset)
        for uid, rows in week.days.items():
            for day_, p, t, a in rows:
                if day_ != d:
                    continue
                room = cap - dealt.get(uid, 0)
                hit = max(0, min(room, damage(week.boss, p, t, a)))
                dealt[uid] = dealt.get(uid, 0) + hit
                total += hit
        if defeated_on is None and total >= week.hp:
            defeated_on = d
    return dealt, total, defeated_on


async def build_raid(db: AsyncSession, viewer_id: str | None = None) -> RaidResponse:
    today = datetime.now(UTC).date()
    this_week = week_start(today)
    earliest = this_week - timedelta(weeks=HISTORY_WEEKS + LOOKBACK_WEEKS)

    rows = (
        await db.execute(
            select(
                DailyStat.user_id, DailyStat.stat_date,
                DailyStat.prompts, DailyStat.tools, DailyStat.artifacts,
            )
            .join(User, User.id == DailyStat.user_id)
            .where(
                DailyStat.stat_date >= earliest,
                DailyStat.stat_date <= today,
                User.is_active.is_(True),
            )
        )
    ).all()

    by_week: dict[date, dict[str, list[tuple[date, int, int, int]]]] = {}
    for uid, d, p, t, a in rows:
        if not (p or t or a):
            continue
        by_week.setdefault(week_start(d), {}).setdefault(uid, []).append(
            (d, int(p), int(t), int(a))
        )

    def team_xp(start: date) -> int:
        return sum(
            _raw_xp(p, t, a)
            for days in by_week.get(start, {}).values()
            for _, p, t, a in days
        )

    def make_week(start: date) -> _Week:
        prior = [start - timedelta(weeks=i) for i in range(1, LOOKBACK_WEEKS + 1)]
        recent = {uid for w in prior for uid in by_week.get(w, {})}
        current = set(by_week.get(start, {}))
        raiders = len(recent | current)
        history = [x for x in (team_xp(w) for w in prior) if x > 0]
        # Raiders for HP come from the lookback only, so a friend joining
        # mid-week makes the fight easier rather than moving the goalposts.
        return _Week(
            start=start,
            boss=boss_for_week(start),
            hp=boss_hp(len(recent) or raiders, history),
            days=by_week.get(start, {}),
            raider_count=raiders,
        )

    week = make_week(this_week)
    dealt, total, defeated_on = _settle(week)

    users = {
        u.id: u
        for u in (
            await db.execute(select(User).where(User.id.in_(list(dealt) or [""])))
        ).scalars()
    }

    # Past raids: who won them, and the viewer's trophy count and raid streak.
    history: list[RaidHistoryItem] = []
    trophies = 0
    team_streak = 0
    streak_open = True
    for i in range(1, HISTORY_WEEKS + 1):
        start = this_week - timedelta(weeks=i)
        past = make_week(start)
        past_dealt, past_total, past_defeated = _settle(past)
        won = past_defeated is not None
        if won and viewer_id and past_dealt.get(viewer_id, 0) > 0:
            trophies += 1
        if streak_open:
            if won:
                team_streak += 1
            else:
                streak_open = False
        history.append(RaidHistoryItem(
            weekStart=start,
            bossName=past.boss.name,
            emoji=past.boss.emoji,
            hp=past.hp,
            damage=min(past_total, past.hp),
            defeated=won,
            raiders=sum(1 for v in past_dealt.values() if v > 0),
        ))

    top = max(dealt.values(), default=0)
    raiders: list[RaidRaider] = []
    for uid, dmg in dealt.items():
        user = users.get(uid)
        if user is None or dmg <= 0:
            continue
        today_hit = sum(
            damage(week.boss, p, t, a) for d, p, t, a in week.days.get(uid, []) if d == today
        )
        raiders.append(RaidRaider(
            handle=user.handle,
            displayName=user.display_name or user.handle,
            trainerName=user.trainer_name,
            avatarUrl=user.avatar_url,
            damage=dmg,
            share=round(dmg / week.hp, 4) if week.hp else 0.0,
            todayDamage=today_hit,
            capped=dmg >= share_cap(week.hp, week.raider_count),
            mvp=dmg == top and top > 0,
            isYou=uid == viewer_id,
        ))
    raiders.sort(key=lambda r: (-r.damage, r.handle))

    ends = this_week + timedelta(days=6)
    return RaidResponse(
        weekStart=this_week,
        weekEnd=ends,
        endsAt=datetime.combine(ends + timedelta(days=1), datetime.min.time(), UTC).isoformat(),
        boss=RaidBoss(
            slug=week.boss.slug,
            name=week.boss.name,
            emoji=week.boss.emoji,
            weakness=week.boss.weakness,
            flavor=week.boss.flavor,
            multiplier=WEAKNESS_MULTIPLIER,
        ),
        hp=week.hp,
        damage=min(total, week.hp),
        defeated=defeated_on is not None,
        defeatedOn=defeated_on,
        raiderCap=share_cap(week.hp, week.raider_count),
        raiders=raiders,
        history=history,
        yourTrophies=trophies,
        teamStreak=team_streak,
        generatedAt=datetime.now(UTC).isoformat(),
    )
