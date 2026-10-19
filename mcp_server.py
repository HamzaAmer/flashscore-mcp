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
