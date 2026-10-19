import asyncio
import os
import re

import httpx

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

mcp = MCPServer("Flashscore Football")

FLASHSCORE_BASE_URL = os.getenv(
    "FLASHSCORE_BASE_URL",
    "http://127.0.0.1:8000",
)

PORT = int(os.getenv("PORT", "3001"))

PLAYER_STAT_KEYWORDS = (
    "minute",
    "goal",
    "assist",
    "shot",
    "expected goal",
    "xg",
    "expected assist",
    "xa",
    "pass",
    "chance",
    "touch",
    "tackle",
    "interception",
    "recover",
    "duel",
    "dribble",
    "clearance",
    "block",
    "save",
    "foul",
    "offside",
    "possession",
)

MATCH_STAT_KEYWORDS = (
    "expected goal",
    "xg",
    "possession",
    "shot",
    "big chance",
    "pass",
    "touch",
    "expected assist",
    "xa",
    "corner",
    "foul",
    "yellow",
    "red",
    "offside",
    "save",
    "tackle",
    "interception",
    "recover",
    "duel",
)


async def flashscore_get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{FLASHSCORE_BASE_URL}{path}",
            params=params or {},
        )
        response.raise_for_status()
        return response.json()


def _normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _team_candidates(search_data: dict, name: str, country: str | None = None):
    wanted_name = _normalized(name)
    wanted_country = _normalized(country)

    candidates = []
    for item in search_data.get("results", []):
        if item.get("type") != "team":
            continue
        if item.get("sport", {}).get("slug") != "football":
            continue

        item_name = _normalized(item.get("name"))
        item_country = _normalized(item.get("country", {}).get("name"))
        gender = _normalized(item.get("gender"))

        score = 0
        if item_name == wanted_name:
            score += 100
        elif item_name.startswith(wanted_name) or wanted_name.startswith(item_name):
            score += 50
        elif wanted_name in item_name:
            score += 25

        if wanted_country:
            if item_country == wanted_country:
                score += 40
            else:
                score -= 20

        if gender == "men":
            score += 5

        candidates.append((score, item))

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in candidates]


async def _resolve_team(name: str, country: str | None = None):
    data = await flashscore_get("/search", {"query": name})
    candidates = _team_candidates(data, name, country)

    if not candidates:
        return None, []

    return candidates[0], candidates[:5]


def _flatten_team_results(data: dict):
    """
    Flatten competition groups back into one chronological list.

    Flashscore groups team results by competition, which destroys the global
    newest-first order. Re-sort after flattening so "last N matches" really
    means the last N across all competitions.
    """
    matches = []
    for group in data.get("competitions", []) or []:
        competition = group.get("competition") or {}
        for match in group.get("matches", []) or []:
            item = dict(match)
            item["competition"] = competition
            matches.append(item)

    matches.sort(
        key=lambda item: (
            item.get("start_time") or "",
            item.get("date") or "",
            item.get("id") or "",
        ),
        reverse=True,
    )
    return matches


def _ref_name(ref):
    if isinstance(ref, dict):
        return ref.get("name")
    return ref


def _compact_match(match: dict, team_id: str | None = None):
    competition = match.get("competition") or {}
    home = match.get("home") or {}
    away = match.get("away") or {}

    home_id = home.get("id") if isinstance(home, dict) else None
    away_id = away.get("id") if isinstance(away, dict) else None

    team_side = None
    if team_id and _normalized(home_id) == _normalized(team_id):
        team_side = "home"
    elif team_id and _normalized(away_id) == _normalized(team_id):
        team_side = "away"

    home_score = match.get("home_score")
    away_score = match.get("away_score")
    result = None
    team_score = None
    opponent_score = None
    opponent = None
    opponent_id = None

    if team_side == "home":
        team_score, opponent_score = home_score, away_score
        opponent, opponent_id = _ref_name(away), away_id
    elif team_side == "away":
        team_score, opponent_score = away_score, home_score
        opponent, opponent_id = _ref_name(home), home_id

    if team_score is not None and opponent_score is not None:
        if team_score > opponent_score:
            result = "win"
        elif team_score < opponent_score:
            result = "loss"
        else:
            result = "draw"

    return {
        "id": match.get("id"),
        "date": match.get("date"),
        "start_time": match.get("start_time"),
        "competition": competition.get("name"),
        "round": match.get("round"),
        "status": match.get("status"),
        "home": _ref_name(home),
        "away": _ref_name(away),
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "winner": match.get("winner"),
        "team_side": team_side,
        "opponent": opponent,
        "opponent_team_id": opponent_id,
        "team_score": team_score,
        "opponent_score": opponent_score,
        "result": result,
        "has_statistics": bool(match.get("has_statistics")),
    }


def _pick_match_period(stat_data: dict):
    periods = stat_data.get("periods", []) or []
    if not periods:
        return None

    preferred = (
        "match",
        "full time",
        "full-time",
        "90 minutes",
        "overall",
    )

    for wanted in preferred:
        for period in periods:
            if _normalized(period.get("period")) == wanted:
                return period

    return periods[0]


def _compact_match_statistics(stat_data: dict, team_side: str | None = None):
    """
    Compact match statistics and, when possible, normalize them to
    team/opponent orientation. This prevents clients from accidentally
    reversing home/away values for away matches.
    """
    period = _pick_match_period(stat_data)
    if not period:
        return {
            "period": None,
            "team_side": team_side,
            "statistics": {},
        }

    compact = {}
    for group in period.get("groups", []) or []:
        for stat in group.get("statistics", []) or []:
            name = stat.get("name")
            normalized_name = _normalized(name)
            if not any(keyword in normalized_name for keyword in MATCH_STAT_KEYWORDS):
                continue

            home = stat.get("home")
            away = stat.get("away")
            home_value = stat.get("home_value")
            away_value = stat.get("away_value")

            row = {
                "home": home,
                "away": away,
                "home_value": home_value,
                "away_value": away_value,
            }

            if team_side == "home":
                row.update({
                    "team": home,
                    "opponent": away,
                    "team_value": home_value,
                    "opponent_value": away_value,
                })
            elif team_side == "away":
                row.update({
                    "team": away,
                    "opponent": home,
                    "team_value": away_value,
                    "opponent_value": home_value,
                })

            compact[name or stat.get("id") or "unknown"] = row

    return {
        "period": period.get("period"),
        "team_side": team_side,
        "statistics": compact,
    }


def _compact_player_entry(player_entry: dict):
    player = player_entry.get("player") or {}
    team = player_entry.get("team") or {}

    selected_stats = {}
    for stat in player_entry.get("statistics", []) or []:
        name = stat.get("name")
        normalized_name = _normalized(name)
        if not any(keyword in normalized_name for keyword in PLAYER_STAT_KEYWORDS):
            continue

        selected_stats[name or stat.get("id") or "unknown"] = (
            stat.get("numeric_value")
            if stat.get("numeric_value") is not None
            else stat.get("value")
        )

        if len(selected_stats) >= 24:
            break

    minutes_played = None
    for key, value in selected_stats.items():
        if "minute" in _normalized(key):
            if isinstance(value, (int, float)):
                minutes_played = value
                break

    if minutes_played is not None:
        participation = "played" if minutes_played > 0 else "unused"
        played = minutes_played > 0
    elif player_entry.get("rating") is not None:
        participation = "played"
        played = True
    else:
        participation = "unknown"
        played = None

    return {
        "player_id": player.get("id") if isinstance(player, dict) else None,
        "name": player.get("name") if isinstance(player, dict) else player,
        "team_id": team.get("id") if isinstance(team, dict) else None,
        "team": team.get("name") if isinstance(team, dict) else team,
        "side": player_entry.get("side"),
        "position": player_entry.get("position"),
        "is_goalkeeper": player_entry.get("is_goalkeeper"),
        "is_starter": player_entry.get("is_starter"),
        "rating": player_entry.get("rating"),
        "is_best_rated": player_entry.get("is_best_rated"),
        "minutes_played": minutes_played,
        "participation": participation,
        "played": played,
        "rating_note": (
            "A null rating does not prove the player was unused. "
            "Use played/participation instead."
        ),
        "statistics": selected_stats,
    }


def _compact_player_statistics(
    data: dict,
    team_id: str | None = None,
    team_name: str | None = None,
    max_players: int = 20,
):
    wanted_team_id = _normalized(team_id)
    wanted_team_name = _normalized(team_name)

    selected = []
    for entry in data.get("players", []) or []:
        team = entry.get("team") or {}
        current_team_id = _normalized(team.get("id") if isinstance(team, dict) else None)
        current_team_name = _normalized(team.get("name") if isinstance(team, dict) else team)

        if wanted_team_id and current_team_id != wanted_team_id:
            continue
        if not wanted_team_id and wanted_team_name and current_team_name != wanted_team_name:
            continue

        selected.append(_compact_player_entry(entry))

    selected.sort(
        key=lambda item: (
            item.get("is_starter") is not True,
            -(item.get("rating") or 0),
            item.get("name") or "",
        )
    )

    selected = selected[:max(1, min(max_players, 25))]

    return {
        "match_id": data.get("id"),
        "date": data.get("date"),
        "home_score": data.get("home_score"),
        "away_score": data.get("away_score"),
        "team_id": team_id,
        "team_name": team_name,
        "player_count": len(selected),
        "players": selected,
    }


def _to_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _stat_lookup(compact_statistics: dict | None, keywords: tuple[str, ...]):
    if not compact_statistics:
        return None, None
    stats = compact_statistics.get("statistics") or {}
    for name, row in stats.items():
        normalized = _normalized(name)
        if all(keyword in normalized for keyword in keywords):
            return _to_number(row.get("team")), _to_number(row.get("opponent"))
    return None, None


def _aggregate_team_form(matches: list[dict]):
    completed = [m for m in matches if m.get("team_score") is not None and m.get("opponent_score") is not None]
    if not completed:
        return {
            "matches": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
        }

    wins = sum(1 for m in completed if m.get("result") == "win")
    draws = sum(1 for m in completed if m.get("result") == "draw")
    losses = sum(1 for m in completed if m.get("result") == "loss")
    goals_for = sum(float(m.get("team_score") or 0) for m in completed)
    goals_against = sum(float(m.get("opponent_score") or 0) for m in completed)

    buckets = {
        "xg_for": [],
        "xg_against": [],
        "possession": [],
        "shots_for": [],
        "shots_against": [],
        "shots_on_target_for": [],
        "shots_on_target_against": [],
        "big_chances_for": [],
        "big_chances_against": [],
        "pass_accuracy": [],
    }

    for match in completed:
        stats = match.get("statistics")
        pairs = {
            "xg": _stat_lookup(stats, ("expected goal",)),
            "possession": _stat_lookup(stats, ("possession",)),
            "shots": _stat_lookup(stats, ("total attempts",)),
            "shots_alt": _stat_lookup(stats, ("shots",)),
            "shots_on_target": _stat_lookup(stats, ("shots on target",)),
            "big_chances": _stat_lookup(stats, ("big chances",)),
            "pass_accuracy": _stat_lookup(stats, ("passes",)),
        }

        xgf, xga = pairs["xg"]
        if xgf is None:
            xgf, xga = _stat_lookup(stats, ("xg",))
        if xgf is not None:
            buckets["xg_for"].append(xgf)
            buckets["xg_against"].append(xga or 0)

        pf, _ = pairs["possession"]
        if pf is not None:
            buckets["possession"].append(pf)

        sf, sa = pairs["shots"]
        if sf is None:
            sf, sa = pairs["shots_alt"]
        if sf is not None:
            buckets["shots_for"].append(sf)
            buckets["shots_against"].append(sa or 0)

        sotf, sota = pairs["shots_on_target"]
        if sotf is not None:
            buckets["shots_on_target_for"].append(sotf)
            buckets["shots_on_target_against"].append(sota or 0)

        bcf, bca = pairs["big_chances"]
        if bcf is not None:
            buckets["big_chances_for"].append(bcf)
            buckets["big_chances_against"].append(bca or 0)

        paf, _ = pairs["pass_accuracy"]
        if paf is not None:
            buckets["pass_accuracy"].append(paf)

    def avg(values):
        return round(sum(values) / len(values), 2) if values else None

    return {
        "matches": len(completed),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points_per_match": round((wins * 3 + draws) / len(completed), 2),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goals_for_per_match": round(goals_for / len(completed), 2),
        "goals_against_per_match": round(goals_against / len(completed), 2),
        "goal_difference_per_match": round((goals_for - goals_against) / len(completed), 2),
        "avg_xg_for": avg(buckets["xg_for"]),
        "avg_xg_against": avg(buckets["xg_against"]),
        "avg_possession": avg(buckets["possession"]),
        "avg_shots_for": avg(buckets["shots_for"]),
        "avg_shots_against": avg(buckets["shots_against"]),
        "avg_shots_on_target_for": avg(buckets["shots_on_target_for"]),
        "avg_shots_on_target_against": avg(buckets["shots_on_target_against"]),
        "avg_big_chances_for": avg(buckets["big_chances_for"]),
        "avg_big_chances_against": avg(buckets["big_chances_against"]),
        "avg_pass_accuracy": avg(buckets["pass_accuracy"]),
    }


def _profile_strengths_weaknesses(form: dict):
    strengths = []
    weaknesses = []

    if (form.get("points_per_match") or 0) >= 2.2:
        strengths.append("strong recent results")
    if (form.get("goals_for_per_match") or 0) >= 2.0:
        strengths.append("high scoring output")
    if form.get("avg_xg_for") is not None and form["avg_xg_for"] >= 2.0:
        strengths.append("creates high-quality chances consistently")
    if form.get("avg_possession") is not None and form["avg_possession"] >= 60:
        strengths.append("controls possession and territory")
    if form.get("avg_shots_for") is not None and form["avg_shots_for"] >= 16:
        strengths.append("sustains attacking shot volume")
    if form.get("avg_big_chances_for") is not None and form["avg_big_chances_for"] >= 3:
        strengths.append("creates frequent big chances")
    if (form.get("goals_against_per_match") or 99) <= 1.0:
        strengths.append("limits goals conceded")
    if form.get("avg_xg_against") is not None and form["avg_xg_against"] <= 1.0:
        strengths.append("suppresses opponent chance quality")

    if (form.get("goals_against_per_match") or 0) >= 1.6:
        weaknesses.append("concedes too frequently")
    if form.get("avg_xg_against") is not None and form["avg_xg_against"] >= 1.5:
        weaknesses.append("allows too much expected-goal threat")
    if form.get("avg_shots_against") is not None and form["avg_shots_against"] >= 13:
        weaknesses.append("allows high opponent shot volume")
    if form.get("avg_big_chances_against") is not None and form["avg_big_chances_against"] >= 2.5:
        weaknesses.append("vulnerable to big chances")
    if form.get("avg_possession") is not None and form["avg_possession"] <= 45:
        weaknesses.append("often plays without the ball")
    if form.get("points_per_match") is not None and form["points_per_match"] <= 1.2:
        weaknesses.append("weak recent results")

    return {
        "strengths": strengths,
        "weaknesses": weaknesses,
    }


def _key_players_from_compact(data: dict | None, limit: int = 6):
    if not data:
        return []
    players = [p for p in (data.get("players") or []) if p.get("played") is not False]
    players.sort(
        key=lambda p: (
            -(p.get("rating") or 0),
            -(p.get("minutes_played") or 0),
            p.get("name") or "",
        )
    )
    return players[:limit]


def _find_direct_fixture(fixtures_a: dict, fixtures_b: dict, team_a_id: str, team_b_id: str):
    candidates = []
    seen = set()

    for payload in (fixtures_a, fixtures_b):
        for match in _flatten_team_results(payload):
            match_id = match.get("id")
            if not match_id or match_id in seen:
                continue
            seen.add(match_id)

            home = match.get("home") or {}
            away = match.get("away") or {}
            ids = {
                str(home.get("id") if isinstance(home, dict) else ""),
                str(away.get("id") if isinstance(away, dict) else ""),
            }

            if str(team_a_id) in ids and str(team_b_id) in ids:
                candidates.append(match)

    candidates.sort(
        key=lambda m: (
            m.get("start_time") or "9999",
            m.get("date") or "9999",
        )
    )
    return candidates[0] if candidates else None


def _prediction_score(form: dict):
    ppg = form.get("points_per_match") or 0
    gd = form.get("goal_difference_per_match") or 0
    xgd = 0
    if form.get("avg_xg_for") is not None and form.get("avg_xg_against") is not None:
        xgd = form["avg_xg_for"] - form["avg_xg_against"]
    shot_edge = 0
    if form.get("avg_shots_for") is not None and form.get("avg_shots_against") is not None:
        shot_edge = (form["avg_shots_for"] - form["avg_shots_against"]) / 10

    return (ppg * 0.9) + (gd * 0.7) + (xgd * 0.8) + (shot_edge * 0.3)


def _prediction_probabilities(score_a: float, score_b: float, home_side: str | None):
    if home_side == "a":
        score_a += 0.20
    elif home_side == "b":
        score_b += 0.20

    diff = score_a - score_b
    draw = max(0.16, min(0.32, 0.28 - abs(diff) * 0.035))
    remaining = 1.0 - draw

    # Smooth deterministic conversion from score gap to win shares.
    import math
    a_share = 1.0 / (1.0 + math.exp(-0.85 * diff))
    a_win = remaining * a_share
    b_win = remaining * (1.0 - a_share)

    return {
        "team_a_win_pct": round(a_win * 100, 1),
        "draw_pct": round(draw * 100, 1),
        "team_b_win_pct": round(b_win * 100, 1),
    }


def _predicted_scoreline(form_a: dict, form_b: dict, probs: dict):
    attack_a = form_a.get("avg_xg_for") or form_a.get("goals_for_per_match") or 1.2
    defense_b = form_b.get("avg_xg_against") or form_b.get("goals_against_per_match") or 1.2
    attack_b = form_b.get("avg_xg_for") or form_b.get("goals_for_per_match") or 1.2
    defense_a = form_a.get("avg_xg_against") or form_a.get("goals_against_per_match") or 1.2

    expected_a = max(0.2, min(4.0, (attack_a + defense_b) / 2))
    expected_b = max(0.2, min(4.0, (attack_b + defense_a) / 2))

    goals_a = int(round(expected_a))
    goals_b = int(round(expected_b))

    # Break an accidental draw toward the higher win probability.
    if goals_a == goals_b:
        if probs["team_a_win_pct"] - probs["team_b_win_pct"] >= 8:
            goals_a += 1
        elif probs["team_b_win_pct"] - probs["team_a_win_pct"] >= 8:
            goals_b += 1

    return {
        "team_a_goals": goals_a,
        "team_b_goals": goals_b,
        "expected_goals_proxy": {
            "team_a": round(expected_a, 2),
            "team_b": round(expected_b, 2),
        },
    }


async def _build_team_recent_dataset(team: dict, match_limit: int):
    team_id = team.get("id")
    name = team.get("name")

    results_data = await flashscore_get(
        "/teams/results",
        {"team": team_id, "page": 1},
    )
    recent_raw = _flatten_team_results(results_data)[:match_limit]

    async def enrich(match):
        compact = _compact_match(match, team_id=team_id)
        if not match.get("has_statistics") or not match.get("id"):
            compact["statistics"] = None
            return compact
        try:
            stats = await flashscore_get(
                "/matches/statistics",
                {"match": match.get("id")},
            )
            compact["statistics"] = _compact_match_statistics(
                stats,
                team_side=compact.get("team_side"),
            )
        except Exception as exc:
            compact["statistics"] = {"error": type(exc).__name__}
        return compact

    enriched = await asyncio.gather(*(enrich(m) for m in recent_raw))

    latest_players = None
    if recent_raw and recent_raw[0].get("id"):
        try:
            raw_players = await flashscore_get(
                "/matches/player-statistics",
                {"match": recent_raw[0].get("id")},
            )
            latest_players = _compact_player_statistics(
                raw_players,
                team_id=team_id,
                team_name=name,
                max_players=25,
            )
        except Exception as exc:
            latest_players = {"error": type(exc).__name__}

    form = _aggregate_team_form(enriched)
    profile = _profile_strengths_weaknesses(form)

    return {
        "team": team,
        "recent_matches": enriched,
        "form": form,
        "profile": profile,
        "key_players_latest_match": _key_players_from_compact(latest_players),
        "latest_match_player_statistics": latest_players,
    }


@mcp.tool()
def ping() -> str:
    """Check whether the Flashscore MCP server is running."""
    return "Flashscore MCP is working"


@mcp.tool()
async def search_team(name: str):
    """
    Search for football teams by name.
    Returns only football teams and ranks exact name matches first.
    """
    data = await flashscore_get("/search", {"query": name})
    results = _team_candidates(data, name)

    return {
        "query": name,
        "count": len(results),
        "results": results,
    }


@mcp.tool()
async def get_team_results(team_id: str, page: int = 1):
    """Get previous match results for a football team."""
    return await flashscore_get(
        "/teams/results",
        {
            "team": team_id,
            "page": page,
        },
    )


@mcp.tool()
async def get_team_fixtures(team_id: str, page: int = 1):
    """Get upcoming fixtures for a football team."""
    return await flashscore_get(
        "/teams/fixtures",
        {
            "team": team_id,
            "page": page,
        },
    )


@mcp.tool()
async def get_team_squad(team_id: str):
    """Get squad and player information for a football team."""
    return await flashscore_get(
        "/teams/squad",
        {
            "team": team_id,
        },
    )


def _raw_lineup_player_index(lineups: dict):
    """Index raw lineup annotations by player ID, deduplicating duplicated sides."""
    index = {}
    raw_side_sets = {"home": set(), "away": set()}

    for raw_side in ("home", "away"):
        payload = lineups.get(raw_side) or {}
        for group in payload.get("groups", []) or []:
            for item in group.get("players", []) or []:
                player = item.get("player") or {}
                player_id = player.get("id") if isinstance(player, dict) else None
                if not player_id:
                    continue
                player_id = str(player_id)
                raw_side_sets[raw_side].add(player_id)
                index.setdefault(player_id, item)

    return index, raw_side_sets


def _entry_minutes_and_participation(entry: dict):
    """Return conservative participation evidence from player-statistics."""
    minutes_played = None
    for stat in entry.get("statistics", []) or []:
        name = _normalized(stat.get("name"))
        if "minute" not in name:
            continue
        value = stat.get("numeric_value")
        if value is None:
            value = stat.get("value")
        if isinstance(value, (int, float)):
            minutes_played = value
            break
        try:
            minutes_played = float(value)
            break
        except (TypeError, ValueError):
            pass

    if minutes_played is not None:
        played = minutes_played > 0
        participation = "played" if played else "unused"
    elif entry.get("rating") is not None:
        played = True
        participation = "played"
    else:
        played = None
        participation = "unknown"

    return minutes_played, played, participation


def _raw_side_validation(lineups: dict, player_stats: dict, home_team_id: str, away_team_id: str):
    """Measure whether each raw lineup side actually belongs to the expected team."""
    player_team = {}
    for entry in player_stats.get("players", []) or []:
        player = entry.get("player") or {}
        team = entry.get("team") or {}
        pid = player.get("id") if isinstance(player, dict) else None
        tid = team.get("id") if isinstance(team, dict) else None
        if pid and tid:
            player_team[str(pid)] = str(tid)

    validation = {}
    for raw_side, expected_team_id in (("home", home_team_id), ("away", away_team_id)):
        ids = []
        payload = lineups.get(raw_side) or {}
        for group in payload.get("groups", []) or []:
            for item in group.get("players", []) or []:
                player = item.get("player") or {}
                pid = player.get("id") if isinstance(player, dict) else None
                if pid:
                    ids.append(str(pid))

        matched = sum(1 for pid in ids if player_team.get(pid) == expected_team_id)
        opposite_team_id = away_team_id if raw_side == "home" else home_team_id
        opposite = sum(1 for pid in ids if player_team.get(pid) == opposite_team_id)
        known = matched + opposite
        purity = (matched / known) if known else 0.0

        validation[raw_side] = {
            "raw_player_count": len(ids),
            "known_player_count": known,
            "expected_team_matches": matched,
            "opposite_team_matches": opposite,
            "purity": round(purity, 3),
        }

    return validation


def _authoritative_lineups(details: dict, lineups: dict, player_stats: dict):
    """
    Rebuild the two team lineups from player-statistics team IDs.

    The raw Flashscore lineup feed can duplicate or reverse its home/away
    groups. Team membership from player-statistics is more reliable, so raw
    lineup data is used only as optional per-player annotation.
    """
    home_team = details.get("home") or {}
    away_team = details.get("away") or {}
    home_team_id = str(home_team.get("id") or "")
    away_team_id = str(away_team.get("id") or "")

    raw_index, raw_side_sets = _raw_lineup_player_index(lineups)

    rebuilt = {
        "home": {
            "team": home_team,
            "formation": None,
            "team_rating": None,
            "groups": [
                {"name": "Starting XI", "players": []},
                {"name": "Substitutes / other squad", "players": []},
            ],
        },
        "away": {
            "team": away_team,
            "formation": None,
            "team_rating": None,
            "groups": [
                {"name": "Starting XI", "players": []},
                {"name": "Substitutes / other squad", "players": []},
            ],
        },
    }

    assigned = {"home": 0, "away": 0}
    unassigned = []

    for entry in player_stats.get("players", []) or []:
        player_ref = entry.get("player") or {}
        team_ref = entry.get("team") or {}
        player_id = player_ref.get("id") if isinstance(player_ref, dict) else None
        team_id = team_ref.get("id") if isinstance(team_ref, dict) else None

        if not player_id:
            continue

        player_id = str(player_id)
        team_id = str(team_id or "")
        raw = raw_index.get(player_id) or {}

        if team_id == home_team_id:
            side = "home"
        elif team_id == away_team_id:
            side = "away"
        else:
            unassigned.append({
                "player_id": player_id,
                "player": player_ref,
                "team": team_ref,
            })
            continue

        rating = entry.get("rating")
        if rating is None:
            rating = raw.get("rating")

        minutes_played, played, participation = _entry_minutes_and_participation(entry)

        player = {
            "player": player_ref,
            "team": team_ref,
            "shirt_number": raw.get("shirt_number"),
            "surname": raw.get("surname"),
            "country": raw.get("country"),
            "position": entry.get("position"),
            "role": raw.get("role"),
            "is_captain": raw.get("is_captain"),
            "is_goalkeeper": entry.get("is_goalkeeper"),
            "is_starter": entry.get("is_starter"),
            "rating": rating,
            "is_best_rated": entry.get("is_best_rated"),
            "minutes_played": minutes_played,
            "played": played,
            "participation": participation,
            "raw_lineup_incident": raw.get("incident"),
        }

        group_index = 0 if entry.get("is_starter") else 1
        rebuilt[side]["groups"][group_index]["players"].append(player)
        assigned[side] += 1

    overlap = len(raw_side_sets["home"] & raw_side_sets["away"])
    smaller = min(len(raw_side_sets["home"]), len(raw_side_sets["away"])) or 1
    overlap_ratio = overlap / smaller

    raw_validation = _raw_side_validation(
        lineups,
        player_stats,
        home_team_id,
        away_team_id,
    )
    raw_sides_trustworthy = (
        overlap_ratio < 0.25
        and raw_validation["home"]["purity"] >= 0.90
        and raw_validation["away"]["purity"] >= 0.90
        and raw_validation["home"]["known_player_count"] >= 10
        and raw_validation["away"]["known_player_count"] >= 10
    )

    if raw_sides_trustworthy:
        for side in ("home", "away"):
            raw_side = lineups.get(side) or {}
            rebuilt[side]["formation"] = raw_side.get("formation")
            rebuilt[side]["team_rating"] = raw_side.get("rating")

    return {
        "id": details.get("id") or lineups.get("id"),
        "date": details.get("date") or lineups.get("date"),
        "home_score": details.get("home_score"),
        "away_score": details.get("away_score"),
        "home_team": home_team,
        "away_team": away_team,
        "home": rebuilt["home"],
        "away": rebuilt["away"],
        "lineup_quality": {
            "assignment_source": "player_statistics.team.id",
            "home_players_assigned": assigned["home"],
            "away_players_assigned": assigned["away"],
            "unassigned_players": unassigned,
            "raw_home_player_count": len(raw_side_sets["home"]),
            "raw_away_player_count": len(raw_side_sets["away"]),
            "raw_side_overlap_count": overlap,
            "raw_side_overlap_ratio": round(overlap_ratio, 3),
            "raw_side_validation": raw_validation,
            "raw_sides_trustworthy": raw_sides_trustworthy,
            "raw_formation_and_team_rating_used": raw_sides_trustworthy,
        },
        "mapping_note": (
            "home/away player membership is rebuilt from player-statistics "
            "team IDs. Raw lineup sides are never used as team identity."
        ),
    }


def _is_goal_incident(incident: dict) -> bool:
    """
    Conservatively identify scoring incidents.

    The Flashscore summary feed sometimes leaves home_score/away_score empty,
    but its incident type/description still identifies goals. Exclude obvious
    non-scoring goal-related events such as disallowed or missed goals.
    """
    text = " ".join(
        str(incident.get(key) or "")
        for key in ("type", "reason", "description")
    )
    normalized = _normalized(text)

    if any(token in normalized for token in (
        "disallowed",
        "no goal",
        "goal cancelled",
        "goal canceled",
        "missed penalty",
        "penalty missed",
    )):
        return False

    return "goal" in normalized


def _score_from_description(description: str | None):
    """Extract the last score token such as 2:4 from an incident description."""
    if not description:
        return None
    matches = re.findall(r"(?<!\\d)(\\d{1,2})\\s*[:\\-]\\s*(\\d{1,2})(?!\\d)", str(description))
    if not matches:
        return None
    home, away = matches[-1]
    return {"home": int(home), "away": int(away)}


def _normalize_match_summary(details: dict, summary: dict):
    """
    Attach authoritative team identity to incidents and derive a conservative
    score timeline. When raw score fields are missing, count goal incidents by
    authoritative team side instead of trusting ambiguous period markers.
    """
    home_team = details.get("home") or {}
    away_team = details.get("away") or {}

    normalized_periods = []
    all_incidents = []
    running_score = {"home": 0, "away": 0}

    for period_index, period in enumerate(summary.get("periods", []) or []):
        normalized_incidents = []

        for incident in period.get("incidents", []) or []:
            item = dict(incident)
            raw_side = item.get("team")

            if raw_side == "home":
                item["team_id"] = home_team.get("id")
                item["team_name"] = home_team.get("name")
            elif raw_side == "away":
                item["team_id"] = away_team.get("id")
                item["team_name"] = away_team.get("name")
            else:
                item["team_id"] = None
                item["team_name"] = None

            numeric_score = None
            if item.get("home_score") is not None and item.get("away_score") is not None:
                numeric_score = {
                    "home": item.get("home_score"),
                    "away": item.get("away_score"),
                }

            parsed_score = _score_from_description(item.get("description"))
            is_goal = _is_goal_incident(item)

            derived_score = None
            if is_goal and raw_side in ("home", "away"):
                running_score[raw_side] += 1
                derived_score = dict(running_score)

            # Prefer an explicit upstream score, then a score token in the
            # description, and finally our goal-count reconstruction.
            effective_score = numeric_score or parsed_score or derived_score

            item["is_goal"] = is_goal
            item["parsed_score_from_description"] = parsed_score
            item["derived_score_from_goal_count"] = derived_score
            item["effective_score"] = effective_score
            item["_period_index"] = period_index

            normalized_incidents.append(item)
            all_incidents.append(item)

        normalized_periods.append({
            "period": period.get("period"),
            "raw_period_score_marker": {
                "home": period.get("home_score_at_start"),
                "away": period.get("away_score_at_start"),
            },
            "incident_derived_end_score": dict(running_score),
            "incidents": normalized_incidents,
        })

    period_checks = []
    for period in normalized_periods:
        period_checks.append({
            "period": period.get("period"),
            "incident_derived_end_score": period.get("incident_derived_end_score"),
            "raw_period_score_marker": period.get("raw_period_score_marker"),
            "marker_semantics": "unknown_upstream_marker",
        })

    final_score = {
        "home": details.get("home_score"),
        "away": details.get("away_score"),
    }

    incident_last_score = dict(running_score) if all_incidents else None
    goal_incident_count = sum(1 for i in all_incidents if i.get("is_goal"))

    warnings = []

    if goal_incident_count == 0:
        warnings.append(
            "No scoring incidents could be identified, so the score timeline "
            "cannot be reconstructed from the incident feed."
        )
    elif incident_last_score != final_score:
        warnings.append(
            "Goal-count reconstruction does not reach the official final score; "
            "one or more scoring incidents may be missing, duplicated or "
            "misclassified."
        )

    for check in period_checks:
        marker = check.get("raw_period_score_marker")
        derived = check.get("incident_derived_end_score")
        if (
            marker
            and derived
            and marker.get("home") is not None
            and marker.get("away") is not None
            and marker != derived
        ):
            warnings.append(
                f"Raw period score marker for {check.get('period') or 'unknown period'} "
                "conflicts with the goal-incident-derived score. The upstream "
                "marker semantics are unreliable and must not be treated as a "
                "period-start or halftime score."
            )

    return {
        "id": summary.get("id") or details.get("id"),
        "home_team": home_team,
        "away_team": away_team,
        "final_score": final_score,
        "periods": normalized_periods,
        "period_score_checks": period_checks,
        "goal_incident_count": goal_incident_count,
        "incident_last_score": incident_last_score,
        "score_consistent_with_final": (
            incident_last_score == final_score if incident_last_score else None
        ),
        "data_quality_warnings": warnings,
        "usage_note": (
            "Use incident team_id/team_name for event ownership. When explicit "
            "incident score fields are absent, derived_score_from_goal_count and "
            "incident_derived_end_score are reconstructed by counting goal "
            "incidents. raw_period_score_marker remains untrusted upstream data."
        ),
    }


@mcp.tool()
async def get_match_lineups(match_id: str):
    """
    Get reconstructed team lineups plus a normalized incident summary.

    Team membership is rebuilt from player-statistics team IDs because the raw
    lineup feed can duplicate or reverse sides. Formation/team rating is
    omitted when the raw sides overlap heavily. Match incidents are normalized
    to authoritative team IDs, and score conflicts are surfaced explicitly.
    """
    details, lineups, player_stats, summary = await asyncio.gather(
        flashscore_get("/matches/details", {"match": match_id}),
        flashscore_get("/matches/lineups", {"match": match_id}),
        flashscore_get("/matches/player-statistics", {"match": match_id}),
        flashscore_get("/matches/summary", {"match": match_id}),
    )

    normalized = _authoritative_lineups(details, lineups, player_stats)
    normalized["summary"] = _normalize_match_summary(details, summary)
    normalized["summary_note"] = (
        "Use summary.periods[].incidents team_id/team_name for event ownership. "
        "If summary.data_quality_warnings is non-empty, report the conflict "
        "rather than guessing which upstream score source is correct."
    )
    return normalized


@mcp.tool()
async def get_match_statistics(match_id: str):
    """Get team statistics such as xG, possession and shots."""
    return await flashscore_get(
        "/matches/statistics",
        {
            "match": match_id,
        },
    )


@mcp.tool()
async def get_match_player_statistics(
    match_id: str,
    team_id: str | None = None,
    team_name: str | None = None,
    max_players: int = 20,
):
    """
    Get compact player statistics and ratings for a match.

    Optional team_id or team_name filters the response to one team, which is
    strongly recommended to avoid oversized responses. Only analysis-relevant
    statistics are returned for each player.
    """
    data = await flashscore_get(
        "/matches/player-statistics",
        {
            "match": match_id,
        },
    )

    return _compact_player_statistics(
        data,
        team_id=team_id,
        team_name=team_name,
        max_players=max_players,
    )


@mcp.tool()
async def analyze_team_recent_matches(
    team_name: str,
    matches: int = 5,
    country: str | None = None,
):
    """
    Build a compact analysis dataset for a team's recent matches.

    Resolves the team name automatically, fetches the most recent results,
    adds key full-time match statistics for each selected match, and includes
    compact player statistics for the most recent match when available.
    The returned payload is intentionally bounded so Claude can analyze it
    without truncation.
    """
    match_limit = max(1, min(int(matches), 5))

    team, alternatives = await _resolve_team(team_name, country)
    if not team:
        return {
            "error": "team_not_found",
            "query": team_name,
            "country": country,
        }

    team_id = team.get("id")
    resolved_name = team.get("name")

    results_data = await flashscore_get(
        "/teams/results",
        {
            "team": team_id,
            "page": 1,
        },
    )

    recent = _flatten_team_results(results_data)[:match_limit]

    async def enrich_match(match):
        compact = _compact_match(match, team_id=team_id)

        if not match.get("has_statistics") or not match.get("id"):
            compact["statistics"] = None
            return compact

        try:
            raw_stats = await flashscore_get(
                "/matches/statistics",
                {"match": match.get("id")},
            )
            compact["statistics"] = _compact_match_statistics(
                raw_stats,
                team_side=compact.get("team_side"),
            )
        except Exception as exc:
            compact["statistics"] = {
                "error": type(exc).__name__,
            }

        return compact

    enriched_matches = await asyncio.gather(
        *(enrich_match(match) for match in recent)
    )

    latest_players = None
    if recent and recent[0].get("id"):
        try:
            raw_players = await flashscore_get(
                "/matches/player-statistics",
                {"match": recent[0].get("id")},
            )
            latest_players = _compact_player_statistics(
                raw_players,
                team_id=team_id,
                team_name=resolved_name,
                max_players=20,
            )
        except Exception as exc:
            latest_players = {
                "error": type(exc).__name__,
                "match_id": recent[0].get("id"),
            }

    return {
        "query": {
            "team_name": team_name,
            "country": country,
            "matches": match_limit,
        },
        "resolved_team": team,
        "alternative_matches": alternatives[1:],
        "recent_match_count": len(enriched_matches),
        "recent_matches": enriched_matches,
        "latest_match_player_statistics": latest_players,
        "analysis_note": (
            "recent_matches is sorted newest-first across ALL competitions. "
            "For each match, team_score/opponent_score and every statistic's "
            "team/opponent fields are already normalized to the resolved team, "
            "so do not infer orientation from home/away. Use "
            "latest_match_player_statistics for the latest-match player view. "
            "Never infer that a player was unused only because rating is null; "
            "use each player's played/participation field."
        ),
    }


@mcp.tool()
async def analyze_matchup(
    team_a: str,
    team_b: str,
    matches: int = 5,
    country_a: str | None = None,
    country_b: str | None = None,
):
    """
    Build one bounded pre-match analysis dataset for two football teams.

    Use this when the user asks who is stronger, each team's strengths and
    weaknesses, how their styles match up, what could decide the game, which
    players matter most, and a reasoned prediction for their next meeting.

    The tool resolves both teams, analyzes recent matches across competitions,
    looks for their next direct fixture, attaches injuries/suspensions and H2H
    when that fixture is available, and returns a transparent heuristic
    prediction. The prediction is an estimate, not a certainty.
    """
    match_limit = max(3, min(int(matches), 8))

    (resolved_a, alternatives_a), (resolved_b, alternatives_b) = await asyncio.gather(
        _resolve_team(team_a, country_a),
        _resolve_team(team_b, country_b),
    )

    if not resolved_a or not resolved_b:
        return {
            "error": "team_not_found",
            "team_a": team_a,
            "team_b": team_b,
            "resolved_team_a": resolved_a,
            "resolved_team_b": resolved_b,
        }

    dataset_a, dataset_b, fixtures_a, fixtures_b = await asyncio.gather(
        _build_team_recent_dataset(resolved_a, match_limit),
        _build_team_recent_dataset(resolved_b, match_limit),
        flashscore_get("/teams/fixtures", {"team": resolved_a.get("id"), "page": 1}),
        flashscore_get("/teams/fixtures", {"team": resolved_b.get("id"), "page": 1}),
    )

    direct_fixture = _find_direct_fixture(
        fixtures_a,
        fixtures_b,
        resolved_a.get("id"),
        resolved_b.get("id"),
    )

    fixture = _compact_match(direct_fixture) if direct_fixture else None
    home_side = None
    if direct_fixture:
        home = direct_fixture.get("home") or {}
        home_id = home.get("id") if isinstance(home, dict) else None
        if str(home_id) == str(resolved_a.get("id")):
            home_side = "a"
        elif str(home_id) == str(resolved_b.get("id")):
            home_side = "b"

    missing_players = None
    h2h = None
    if direct_fixture and direct_fixture.get("id"):
        match_id = direct_fixture.get("id")
        missing_players, h2h = await asyncio.gather(
            flashscore_get("/matches/missing-players", {"match": match_id}),
            flashscore_get("/matches/h2h", {"match": match_id}),
            return_exceptions=True,
        )
        if isinstance(missing_players, Exception):
            missing_players = {"error": type(missing_players).__name__}
        if isinstance(h2h, Exception):
            h2h = {"error": type(h2h).__name__}

    score_a = _prediction_score(dataset_a["form"])
    score_b = _prediction_score(dataset_b["form"])
    probabilities = _prediction_probabilities(score_a, score_b, home_side)
    scoreline = _predicted_scoreline(
        dataset_a["form"],
        dataset_b["form"],
        probabilities,
    )

    tactical_edges = []
    a_form = dataset_a["form"]
    b_form = dataset_b["form"]

    if (
        a_form.get("avg_possession") is not None
        and b_form.get("avg_possession") is not None
    ):
        possession_gap = a_form["avg_possession"] - b_form["avg_possession"]
        if possession_gap >= 8:
            tactical_edges.append({
                "edge": "team_a_possession_control",
                "explanation": (
                    f"{resolved_a.get('name')} has the stronger recent possession "
                    "profile and is more likely to control territory."
                ),
            })
        elif possession_gap <= -8:
            tactical_edges.append({
                "edge": "team_b_possession_control",
                "explanation": (
                    f"{resolved_b.get('name')} has the stronger recent possession "
                    "profile and is more likely to control territory."
                ),
            })

    if (
        a_form.get("avg_xg_for") is not None
        and b_form.get("avg_xg_against") is not None
        and a_form["avg_xg_for"] >= 2.0
        and b_form["avg_xg_against"] >= 1.3
    ):
        tactical_edges.append({
            "edge": "team_a_chance_creation_vs_team_b_defense",
            "explanation": (
                f"{resolved_a.get('name')}'s recent chance creation directly targets "
                f"a defensive weakness in {resolved_b.get('name')}."
            ),
        })

    if (
        b_form.get("avg_xg_for") is not None
        and a_form.get("avg_xg_against") is not None
        and b_form["avg_xg_for"] >= 2.0
        and a_form["avg_xg_against"] >= 1.3
    ):
        tactical_edges.append({
            "edge": "team_b_chance_creation_vs_team_a_defense",
            "explanation": (
                f"{resolved_b.get('name')}'s recent chance creation directly targets "
                f"a defensive weakness in {resolved_a.get('name')}."
            ),
        })

    return {
        "query": {
            "team_a": team_a,
            "team_b": team_b,
            "matches": match_limit,
            "country_a": country_a,
            "country_b": country_b,
        },
        "resolved_team_a": resolved_a,
        "resolved_team_b": resolved_b,
        "alternative_team_a_matches": alternatives_a[1:],
        "alternative_team_b_matches": alternatives_b[1:],
        "next_direct_fixture": fixture,
        "team_a_analysis": dataset_a,
        "team_b_analysis": dataset_b,
        "availability_for_next_fixture": missing_players,
        "head_to_head_for_next_fixture": h2h,
        "tactical_matchup": {
            "data_driven_edges": tactical_edges,
            "coach_analysis_guidance": (
                "Explain the coaching matchup through observable tactical profiles "
                "from the recent-match data. Do not invent a coach name, formation, "
                "or tactical preference that is not present in the tool data. "
                "Frame manager advantages as conditional: what each side needs to "
                "do to exploit the opponent's measurable weaknesses."
            ),
        },
        "prediction": {
            "method": (
                "Transparent heuristic using recent points-per-match, goal difference, "
                "xG difference when available, shot differential, and a small home "
                "advantage when the direct fixture is known."
            ),
            "team_a_model_score": round(score_a, 3),
            "team_b_model_score": round(score_b, 3),
            "probabilities": probabilities,
            "predicted_scoreline": scoreline,
            "confidence_note": (
                "This is a data-based estimate, not a guaranteed result. Claude should "
                "explain the main factors that could invalidate the prediction, "
                "especially injuries, rotation, red cards, and tactical changes."
            ),
        },
        "response_guidance": (
            "For the final answer: compare strengths and weaknesses, explain the "
            "tactical matchup in detail, identify key players and availability, "
            "describe how each coaching approach could gain an edge using only "
            "observable data, then give win/draw/loss probabilities and one predicted "
            "score. Distinguish measured facts from interpretation."
        ),
    }


if __name__ == "__main__":
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORT,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
