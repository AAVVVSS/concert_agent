"""
Research each favorite artist:
  1. Determine if they are still active (alive / band not disbanded).
  2. If active, search for upcoming concerts in the greater Zürich area.

Results are merged back into favorite_artists.json (active status) and
written to upcoming_concerts.json (concert events).

Re-running skips artists whose active status was checked within the last 30 days.
"""

import argparse
import dataclasses
import json
import os
import re
from datetime import datetime, timezone, timedelta

import anthropic
import openai
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tavily import TavilyClient
from tqdm import tqdm

ARTISTS_FILE = "favorite_artists.json"
CONCERTS_FILE = "upcoming_concerts.json"
FAILED_FETCHES_FILE = "failed_fetches.json"
RECHECK_DAYS = 30
MUSICBRAINZ_UA = "concert-agent/0.1 (personal project)"

# Ollama defaults
OLLAMA_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MAIN_MODEL = "qwen3:32b"
DEFAULT_PARSER_MODEL = "qwen3:8b"
MAX_HTML_CHARS = 24_000


@dataclasses.dataclass
class AgentConfig:
    mode: str  # "anthropic" or "ollama"
    anthropic_client: anthropic.Anthropic | None = None
    openai_client: openai.OpenAI | None = None
    parser_client: openai.OpenAI | None = None
    main_model: str = DEFAULT_MAIN_MODEL
    parser_model: str = DEFAULT_PARSER_MODEL
    page_parser_enabled: bool = True


def clean_bio(bio: str | None) -> str:
    """Strip Tidal [wimpLink ...] markup tags from bio text."""
    if not bio:
        return "(No bio available)"
    return re.sub(r"\[/?wimpLink[^\]]*\]", "", bio).strip()


# Accumulates fetch failures during a run; saved at exit.
_failed_fetches: list[dict] = []


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def check_ollama(base_url: str, model: str) -> bool:
    """Return True if Ollama is running and the model is available."""
    try:
        api_base = base_url.rstrip("/v1").rstrip("/")
        resp = requests.get(f"{api_base}/api/tags", timeout=3)
        available = [m["name"] for m in resp.json().get("models", [])]
        return model in available or model in {m.split(":")[0] for m in available}
    except Exception:
        return False


def fetch_page(url: str) -> str:
    """Fetch a web page and return its raw HTML."""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    resp = requests.get(url, timeout=15, headers=headers)
    resp.raise_for_status()
    return resp.text


def html_to_text(html: str) -> str:
    """Strip HTML to plain text, removing boilerplate elements."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "img"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_HTML_CHARS]


def _record_failed_fetch(url: str, artist_name: str, error: str, category: str) -> None:
    """Record a fetch failure for later review."""
    _failed_fetches.append({
        "url": url,
        "artist": artist_name,
        "error": error,
        "category": category,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _tavily_extract(url: str, tavily: TavilyClient | None) -> str | None:
    """Use Tavily extract as a fallback for JS-heavy or blocked pages."""
    if tavily is None:
        return None
    try:
        result = tavily.extract(urls=[url])
        results = result.get("results", [])
        if results and results[0].get("raw_content"):
            text = results[0]["raw_content"]
            return text[:MAX_HTML_CHARS]
    except Exception:
        pass
    return None


def parse_concert_page(url: str, artist_name: str, config: AgentConfig,
                       tavily: TavilyClient | None = None) -> str:
    """Fetch a URL and use a small local model to extract concert info.

    Tries a direct HTTP fetch first. If that fails or returns too little text,
    falls back to Tavily extract (which handles JS rendering server-side).
    """
    text = None

    # Try direct fetch first (free, fast)
    try:
        html = fetch_page(url)
        text = html_to_text(html)
        if len(text.strip()) < 50:
            text = None  # too thin, try fallback
    except Exception:
        pass  # will try fallback

    # Fallback to Tavily extract for JS-heavy or blocked pages
    if text is None:
        text = _tavily_extract(url, tavily)

    # If both methods failed, record and return
    if text is None or len(text.strip()) < 50:
        try:
            # Try the direct fetch once more just to get the error message
            fetch_page(url)
        except requests.exceptions.HTTPError as e:
            _record_failed_fetch(url, artist_name, str(e), "http_error")
            return f"Failed to fetch {url}: {e}"
        except requests.exceptions.Timeout as e:
            _record_failed_fetch(url, artist_name, str(e), "timeout")
            return f"Failed to fetch {url}: {e}"
        except Exception as e:
            _record_failed_fetch(url, artist_name, str(e), "other")
            return f"Failed to fetch {url}: {e}"
        _record_failed_fetch(url, artist_name, "Page contained no meaningful text content", "empty_page")
        return "Page contained no meaningful text content."

    extraction_prompt = (
        f'Extract any concert/event information for "{artist_name}" from the following web page text.\n'
        "Focus on: dates, venues, cities, and ticket URLs.\n"
        "Only include events in Switzerland (especially Zürich, Winterthur, Lucerne, Basel, Bern).\n"
        "Only include events from 2026 onwards.\n"
        'If no relevant concerts are found, say "No relevant concerts found on this page."\n'
        "Return a concise summary of findings. Do not invent information not present in the text.\n\n"
        f"PAGE TEXT:\n{text}"
    )

    response = config.parser_client.chat.completions.create(
        model=config.parser_model,
        messages=[{"role": "user", "content": extraction_prompt}],
        max_tokens=1024,
        temperature=0.1,
    )
    result = response.choices[0].message.content or ""
    if not result.strip():
        return "No relevant concerts found on this page."
    return result


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

When web_search returns URLs that look like concert listings, event pages, or ticket sites,
use parse_concert_page to fetch the full page and extract detailed concert information.
This is especially useful when search snippets are incomplete about dates or venues.
Best results come from venue websites and event aggregators (e.g. songkick.com concert pages,
venue sites like hallenstadion.ch, thehall.ch, dampfzentrale.ch).
Social media and JS-heavy pages (Instagram, Facebook) may also work via a server-side fallback.

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
    {
        "name": "parse_concert_page",
        "description": "Fetch the full content of a web page URL and extract concert/event information for the artist. Use on promising URLs from web_search results to get detailed concert dates, venues, and ticket links.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch and parse"}
            },
            "required": ["url"],
        },
    },
]

# OpenAI function-calling format (for Ollama)
TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "query_musicbrainz",
            "description": "Look up an artist on MusicBrainz to get structured data: type (Person/Group), life-span ended flag, end date, disambiguation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Artist or band name to search for"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information about an artist's activity status or upcoming concert dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_concert_page",
            "description": "Fetch the full content of a web page URL and extract concert/event information for the artist. Use on promising URLs from web_search results to get detailed concert dates, venues, and ticket links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch and parse"}
                },
                "required": ["url"],
            },
        },
    },
]


def get_tools(fmt: str, page_parser_enabled: bool) -> list:
    """Return tool definitions in the specified format, optionally excluding the page parser."""
    if fmt == "anthropic":
        tools = TOOLS
    else:
        tools = TOOLS_OPENAI
    if not page_parser_enabled:
        tools = [t for t in tools if (t.get("name") or t.get("function", {}).get("name")) != "parse_concert_page"]
    return tools


def _dispatch_tool(tool_name: str, args: dict, artist_name: str, config: AgentConfig, tavily: TavilyClient) -> str:
    """Execute a tool call and return the result string."""
    if tool_name == "query_musicbrainz":
        return query_musicbrainz(args["name"])
    elif tool_name == "web_search":
        return web_search(args["query"], tavily)
    elif tool_name == "parse_concert_page":
        return parse_concert_page(args["url"], artist_name, config, tavily)
    return "Unknown tool."


def _parse_json_result(text: str) -> dict | None:
    """Try to extract a JSON object from a text string."""
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except json.JSONDecodeError:
        pass
    return None


FALLBACK_RESULT = {"active": None, "reason": "Could not parse agent response.", "concerts": []}


def _run_anthropic_loop(name: str, bio_text: str, config: AgentConfig, tavily: TavilyClient) -> dict:
    """Agent loop using the Anthropic API."""
    messages = [{"role": "user", "content": f"Artist: {name}\n\nBio:\n{bio_text}"}]
    tools = get_tools("anthropic", config.page_parser_enabled)

    for _ in range(10):
        response = config.anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        text_blocks = [b.text for b in response.content if b.type == "text"]
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "end_turn" or not tool_uses:
            for text in reversed(text_blocks):
                parsed = _parse_json_result(text)
                if parsed is not None:
                    return parsed
            return FALLBACK_RESULT

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tool_use in tool_uses:
            result = _dispatch_tool(tool_use.name, tool_use.input, name, config, tavily)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    return {"active": None, "reason": "Agent did not converge.", "concerts": []}


def _run_openai_loop(name: str, bio_text: str, config: AgentConfig, tavily: TavilyClient) -> dict:
    """Agent loop using the OpenAI-compatible API (Ollama)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Artist: {name}\n\nBio:\n{bio_text}"},
    ]
    tools = get_tools("openai", config.page_parser_enabled)

    for _ in range(10):
        response = config.openai_client.chat.completions.create(
            model=config.main_model,
            messages=messages,
            tools=tools,
            max_tokens=2048,
            temperature=0.3,
        )
        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            if msg.content:
                parsed = _parse_json_result(msg.content)
                if parsed is not None:
                    return parsed
            return FALLBACK_RESULT

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "Error: malformed arguments. Please try again with valid JSON.",
                })
                continue
            result = _dispatch_tool(tc.function.name, args, name, config, tavily)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    return {"active": None, "reason": "Agent did not converge.", "concerts": []}


def research_artist(name: str, bio: str | None, config: AgentConfig, tavily: TavilyClient) -> dict:
    """Run the agent loop for one artist. Returns parsed result dict."""
    bio_text = clean_bio(bio)
    if config.mode == "ollama":
        return _run_openai_loop(name, bio_text, config, tavily)
    return _run_anthropic_loop(name, bio_text, config, tavily)


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
    parser.add_argument("--local", action="store_true", help="Use Ollama for the main agent instead of the Anthropic API")
    parser.add_argument("--main-model", type=str, default=DEFAULT_MAIN_MODEL, help=f"Ollama model for the main agent (default: {DEFAULT_MAIN_MODEL})")
    parser.add_argument("--parser-model", type=str, default=DEFAULT_PARSER_MODEL, help=f"Ollama model for the page parser (default: {DEFAULT_PARSER_MODEL})")
    parser.add_argument("--no-page-parser", action="store_true", help="Disable the page parser tool")
    args = parser.parse_args()

    load_dotenv()
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        raise SystemExit("TAVILY_API_KEY not set in environment / .env")
    tavily = TavilyClient(api_key=tavily_key)

    # --- Build AgentConfig ---
    page_parser_enabled = not args.no_page_parser

    if args.local:
        # Verify Ollama is running and model is available
        if not check_ollama(OLLAMA_BASE_URL, args.main_model):
            raise SystemExit(
                f"Ollama is not running or model '{args.main_model}' is not available.\n"
                f"Start Ollama with: ollama serve\n"
                f"Pull the model with: ollama pull {args.main_model}"
            )
        oi_client = openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=300.0)
        config = AgentConfig(
            mode="ollama",
            openai_client=oi_client,
            main_model=args.main_model,
            parser_model=args.parser_model,
            page_parser_enabled=page_parser_enabled,
        )
        print(f"Using Ollama (main: {args.main_model})")
    else:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise SystemExit("ANTHROPIC_API_KEY not set in environment / .env")
        config = AgentConfig(
            mode="anthropic",
            anthropic_client=anthropic.Anthropic(api_key=anthropic_key),
            page_parser_enabled=page_parser_enabled,
        )

    # Set up page parser client (always uses Ollama)
    if page_parser_enabled:
        if check_ollama(OLLAMA_BASE_URL, args.parser_model):
            config.parser_client = openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=120.0)
            print(f"Page parser enabled (model: {args.parser_model})")
        else:
            config.page_parser_enabled = False
            tqdm.write(f"  Warning: Ollama not available for page parser (model: {args.parser_model}). Disabling page parser.")

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

        result = research_artist(name, entry.get("bio"), config, tavily)

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

    # Merge new fetch failures with existing ones and save
    if _failed_fetches:
        existing_failures = load_json(FAILED_FETCHES_FILE, [])
        # Deduplicate by URL (keep the latest attempt)
        by_url = {f["url"]: f for f in existing_failures}
        for f in _failed_fetches:
            by_url[f["url"]] = f
        save_json(FAILED_FETCHES_FILE, list(by_url.values()))
        tqdm.write(f"Logged {len(_failed_fetches)} fetch failure(s) to {FAILED_FETCHES_FILE}")


if __name__ == "__main__":
    main()
