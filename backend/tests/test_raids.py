from datetime import UTC, date, datetime, timedelta

from app.raids import (
    BOSSES, HP_PER_RAIDER, boss_for_week, boss_hp, damage, share_cap, week_start,
)
from tests.conftest import auth, make_user

TODAY = datetime.now(UTC).date()
THIS_WEEK = week_start(TODAY)
BOSS = boss_for_week(THIS_WEEK)


def day(d: date, prompts=0, tools=0, artifacts=0):
    return {"date": d.isoformat(), "prompts": prompts, "tools": tools, "artifacts": artifacts}


def payload(*days):
    return {"schemaVersion": 1, "days": list(days)}


def test_boss_is_stable_for_a_week_and_rotates():
    assert boss_for_week(THIS_WEEK) == boss_for_week(week_start(THIS_WEEK + timedelta(days=6)))
    seen = {boss_for_week(THIS_WEEK + timedelta(weeks=i)).slug for i in range(len(BOSSES) * 2)}
    assert len(seen) > 1


def test_weakness_counts_double():
    boss = BOSSES[0]  # weak to tools
    assert damage(boss, prompts=1, tools=0, artifacts=0) == 10
    assert damage(boss, prompts=0, tools=1, artifacts=0) == 6


def test_hp_has_a_floor_and_stretches_above_the_recent_average():
    assert boss_hp(0, []) == HP_PER_RAIDER
    assert boss_hp(3, []) == 3 * HP_PER_RAIDER
    # 10k average * 1.1 = 11k, above the 2k floor.
    assert boss_hp(1, [10_000, 10_000]) == 11_000


def test_cap_is_an_even_split_for_small_teams():
    assert share_cap(1000, 1) == 1000
    assert share_cap(1000, 2) == 500
    assert share_cap(1000, 5) == 400


async def test_raid_requires_a_device(client):
    assert client.get("/v1/raid").status_code == 401


async def test_empty_raid_has_a_boss_and_full_hp(client):
    _, token = await make_user("ash", 20)
    raid = client.get("/v1/raid", headers=auth(token)).json()
    assert raid["boss"]["slug"] == BOSS.slug
    assert raid["hp"] == HP_PER_RAIDER
    assert raid["damage"] == 0
    assert raid["defeated"] is False
    assert raid["raiders"] == []
    assert raid["weekStart"] == THIS_WEEK.isoformat()


async def test_activity_this_week_damages_the_boss(client):
    _, token = await make_user("ash", 21)
    client.post("/v1/stats", headers=auth(token), json=payload(day(TODAY, prompts=5, tools=10)))

    raid = client.get("/v1/raid", headers=auth(token)).json()
    expected = damage(BOSS, 5, 10, 0)
    assert raid["damage"] == expected
    [me] = raid["raiders"]
    assert me["handle"] == "ash" and me["isYou"] and me["mvp"]
    assert me["damage"] == me["todayDamage"] == expected


async def test_one_raider_cannot_solo_a_team_boss(client):
    _, heavy = await make_user("gary", 22)
    _, light = await make_user("misty", 23)
    client.post("/v1/stats", headers=auth(heavy), json=payload(day(TODAY, prompts=2000)))
    client.post("/v1/stats", headers=auth(light), json=payload(day(TODAY, prompts=1)))

    raid = client.get("/v1/raid", headers=auth(light)).json()
    assert raid["hp"] == 2 * HP_PER_RAIDER
    gary = next(r for r in raid["raiders"] if r["handle"] == "gary")
    assert gary["capped"] is True
    assert gary["damage"] == raid["raiderCap"] == raid["hp"] // 2
    assert raid["defeated"] is False


async def test_last_weeks_win_shows_in_history_and_trophies(client):
    _, token = await make_user("ash", 24)
    last_week = THIS_WEEK - timedelta(days=7)
    client.post("/v1/stats", headers=auth(token), json=payload(day(last_week, prompts=400)))

    raid = client.get("/v1/raid", headers=auth(token)).json()
    past = raid["history"][0]
    assert past["weekStart"] == last_week.isoformat()
    assert past["defeated"] is True and past["raiders"] == 1
    assert raid["yourTrophies"] == 1
    assert raid["teamStreak"] == 1
    # Last week's activity makes ash a known raider, so this week's HP scales to it.
    assert raid["hp"] >= HP_PER_RAIDER
