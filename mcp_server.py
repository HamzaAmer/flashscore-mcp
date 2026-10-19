import os
import httpx

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

mcp = MCPServer("Flashscore Football")

FLASHSCORE_BASE_URL = os.getenv(
    "FLASHSCORE_BASE_URL",
    "http://127.0.0.1:8000",
)

PORT = int(os.getenv("PORT", "3001"))


async def flashscore_get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{FLASHSCORE_BASE_URL}{path}",
            params=params or {},
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
def ping() -> str:
    """Check whether the Flashscore MCP server is running."""
    return "Flashscore MCP is working"


@mcp.tool()
async def search_team(name: str):
    """
    Search for football teams by name.
    """
    data = await flashscore_get(
        "/search",
        {"query": name},
    )

    results = []

    for item in data.get("results", []):
        if (
            item.get("type") == "team"
            and item.get("sport", {}).get("slug") == "football"
        ):
            results.append(item)

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
async def get_match_player_statistics(match_id: str):
    """Get detailed player statistics and ratings."""
    return await flashscore_get(
        "/matches/player-statistics",
        {
            "match": match_id,
        },
    )


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