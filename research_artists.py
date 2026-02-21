"""
Research each favorite artist:
  1. Determine if they are still active (alive / band not disbanded).
  2. If active, search for upcoming concerts in the greater Zürich area.

Results are merged back into favorite_artists.json (active status) and
written to upcoming_concerts.json (concert events).

Re-running skips artists whose active status was checked within the last 30 days.
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone, timedelta

import anthropic
import requests
from dotenv import load_dotenv
from tavily import TavilyClient
from tqdm import tqdm

ARTISTS_FILE = "favorite_artists.json"
CONCERTS_FILE = "upcoming_concerts.json"
RECHECK_DAYS = 30
MUSICBRAINZ_UA = "concert-agent/0.1 (personal project)"


def clean_bio(bio: str | None) -> str:
    """Strip Tidal [wimpLink ...] markup tags from bio text."""
    if not bio:
        return "(No bio available)"
    return re.sub(r"\[/?wimpLink[^\]]*\]", "", bio).strip()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def query_musicbrainz(name: str) -> str:
    """Call the free MusicBrainz API and return a compact summary string."""
    url = "https://musicbrainz.org/ws/2/artist/"
    params = {"query": f'artist:"{name}"', "fmt": "json", "limit": 3}
    headers = {"User-Agent": MUSICBRAINZ_UA}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        artists = resp.json().get("artists", [])
        if not artists:
            return "No results found on MusicBrainz."
        lines = []
        for a in artists:
            life = a.get("life-span", {})
            lines.append(
                f"Name: {a.get('name')} | Type: {a.get('type')} | "
                f"Ended: {life.get('ended')} | End: {life.get('end', 'N/A')} | "
                f"Disambiguation: {a.get('disambiguation', '')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"MusicBrainz lookup failed: {e}"


def web_search(query: str, tavily: TavilyClient) -> str:
    """Run a Tavily web search and return a compact summary string."""
    try:
        results = tavily.search(query, max_results=5)
        snippets = []
        for r in results.get("results", []):
            snippets.append(f"[{r.get('title')}] {r.get('content', '')} — {r.get('url')}")
        return "\n\n".join(snippets) if snippets else "No results found."
    except Exception as e:
        return f"Web search failed: {e}"


# ---------------------------------------------------------------------------
# Claude agent loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a music research assistant. For each artist you receive, complete TWO tasks:

TASK 1 — ACTIVITY STATUS
Determine if they are still ACTIVE — meaning the band still exists and performs,
or the solo artist is alive. If members have died but the band continues under new
members, they are still active. Consider them INACTIVE only if:
- The band has officially disbanded/split up, OR
- The sole/all key members are deceased and no continuation exists.

Steps for Task 1:
- Read the bio first. If it clearly answers the active question, you may skip tools.
- Use query_musicbrainz if the bio is ambiguous about band status or death.
- Use web_search to confirm ambiguous cases.

TASK 2 — ZÜRICH CONCERTS (only if active=true)
You MUST call web_search specifically for this task. Search for upcoming concerts or
festival appearances in the GREATER ZÜRICH AREA (Switzerland) in 2026 or later.
Include nearby cities: Zürich, Winterthur, Lucerne, Basel, Bern.
Use a query like: '"Artist name" concert OR tour Switzerland OR Zürich 2026'
Also try: '"Artist name" live 2026 Switzerland'
Do NOT skip this search step for active artists.
Do NOT report concerts from other countries.
Do NOT hallucinate dates — only report events explicitly found in search results.
Do NOT fabricate URLs. Only include the actual source URL from the search result. If no credible URL is available, omit the concert entry entirely.

When you are done with both tasks, respond with ONLY this JSON (no markdown, no extra text):
{"active": true|false|null, "permanent": true|false, "reason": "1-2 sentence explanation.", "concerts": [{"date": "YYYY-MM-DD or TBD", "venue": "...", "city": "...", "country": "Switzerland", "url": "..."}]}

Rules for "permanent":
- Set to true when active=false AND the situation is irreversible: a key member has died,
  or the band has definitively disbanded with essentially no realistic chance of reunion.
- Set to false when active=false but reunion/continuation is plausible (e.g. on hiatus,
  members alive, informal split).
- Always false when active=true or null.

Use null for active if genuinely uncertain. Use [] for concerts if none found."""

TOOLS = [
    {
        "name": "query_musicbrainz",
        "description": "Look up an artist on MusicBrainz to get structured data: type (Person/Group), life-span ended flag, end date, disambiguation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Artist or band name to search for"}
            },
            "required": ["name"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web for current information about an artist's activity status or upcoming concert dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"}
            },
            "required": ["query"],
        },
    },
]


def research_artist(name: str, bio: str | None, client: anthropic.Anthropic, tavily: TavilyClient) -> dict:
    """Run the Claude agent loop for one artist. Returns parsed result dict."""
    bio_text = clean_bio(bio)
    user_message = f"Artist: {name}\n\nBio:\n{bio_text}"

    messages = [{"role": "user", "content": user_message}]

    for _ in range(10):  # max tool rounds
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Collect any text blocks as the candidate final answer
        text_blocks = [b.text for b in response.content if b.type == "text"]
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "end_turn" or not tool_uses:
            # Try to parse JSON from the last text block
            for text in reversed(text_blocks):
                try:
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    if match:
                        return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return {"active": None, "reason": "Could not parse agent response.", "concerts": []}

        # Process tool calls
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tool_use in tool_uses:
            if tool_use.name == "query_musicbrainz":
                result = query_musicbrainz(tool_use.input["name"])
            elif tool_use.name == "web_search":
                result = web_search(tool_use.input["query"], tavily)
            else:
                result = "Unknown tool."
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    return {"active": None, "reason": "Agent did not converge.", "concerts": []}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Process at most N artists (for testing)")
    parser.add_argument("--names", type=str, default=None, help="Comma-separated artist names to process (ignores cache)")
    args = parser.parse_args()

    load_dotenv()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not anthropic_key:
        raise SystemExit("ANTHROPIC_API_KEY not set in environment / .env")
    if not tavily_key:
        raise SystemExit("TAVILY_API_KEY not set in environment / .env")

    ai = anthropic.Anthropic(api_key=anthropic_key)
    tavily = TavilyClient(api_key=tavily_key)

    artists: dict = load_json(ARTISTS_FILE, {})
    concerts: list = load_json(CONCERTS_FILE, [])

    # --names: force-research specific artists by name (case-insensitive, bypasses cache)
    if args.names:
        target_names = {n.strip().lower() for n in args.names.split(",")}
        to_research = [
            key for key, entry in artists.items()
            if entry["name"].lower() in target_names
        ]
        if not to_research:
            raise SystemExit(f"No artists matched: {args.names}")
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)
        to_research = []
        for key, entry in artists.items():
            if entry.get("permanently_inactive"):
                tqdm.write(f"  Skipping {entry['name']} (permanently inactive)")
                continue
            checked = entry.get("active_checked_at")
            if checked:
                try:
                    checked_dt = datetime.fromisoformat(checked)
                    if checked_dt > cutoff:
                        tqdm.write(f"  Skipping {entry['name']} (checked {checked[:10]})")
                        continue
                except ValueError:
                    pass
            to_research.append(key)

        if not to_research:
            print("All artists are up to date. Nothing to do.")
            return

        if args.limit:
            to_research = to_research[: args.limit]

    # Remove stale concert entries for artists we're about to re-check
    researched_ids = {artists[k]["id"] for k in to_research}
    concerts = [c for c in concerts if c.get("artist_id") not in researched_ids]

    now = datetime.now(timezone.utc).isoformat()

    pbar = tqdm(to_research, desc="Researching artists", unit="artist")
    for key in pbar:
        entry = artists[key]
        name = entry["name"]
        pbar.set_postfix_str(name)

        result = research_artist(name, entry.get("bio"), ai, tavily)

        entry["active"] = result.get("active")
        entry["active_reason"] = result.get("reason", "")
        entry["active_checked_at"] = now
        if result.get("permanent"):
            entry["permanently_inactive"] = True
        artists[key] = entry

        today = datetime.now(timezone.utc).date()
        for concert in result.get("concerts", []):
            date_str = concert.get("date", "TBD")
            if date_str != "TBD":
                try:
                    concert_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if concert_date < today:
                        tqdm.write(f"  ⚠ Skipping past concert: {name} on {date_str}")
                        continue
                except ValueError:
                    pass
            concerts.append({
                "artist_id": entry["id"],
                "artist_name": name,
                "date": date_str,
                "venue": concert.get("venue", ""),
                "city": concert.get("city", ""),
                "country": concert.get("country", "Switzerland"),
                "url": concert.get("url", ""),
                "searched_at": now,
            })

        if result.get("active"):
            status = "ACTIVE"
        elif result.get("active") is False:
            status = "PERMANENTLY INACTIVE" if result.get("permanent") else "INACTIVE"
        else:
            status = "UNKNOWN"
        concert_count = len(result.get("concerts", []))
        tqdm.write(f"  {name}: {status} | {result.get('reason', '')[:80]} | {concert_count} concert(s)")

    save_json(ARTISTS_FILE, artists)
    tqdm.write(f"\nSaved updated artist data to {ARTISTS_FILE}")

    save_json(CONCERTS_FILE, concerts)
    tqdm.write(f"Saved {len(concerts)} total concert entries to {CONCERTS_FILE}")


if __name__ == "__main__":
    main()
