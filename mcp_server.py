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


@mcp.tool()
async def get_match_lineups(match_id: str):
    """Get match lineups and formations."""
    return await flashscore_get(
        "/matches/lineups",
        {
            "match": match_id,
        },
    )


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
            "latest_match_player_statistics for the latest-match player view."
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
