"""
Test the page fetch + HTML extraction pipeline on real concert URLs.

Tests fetch_page → html_to_text on URLs from the existing concert report,
plus a manual Polyphia search. This validates the non-LLM parts of the
page parser: can we actually fetch and extract readable text from these sites?

The Ollama extraction step (parse_concert_page) requires Ollama running
and is tested separately if available.

Usage:
    python test_pages.py
"""

import os
import sys
import openai

from dotenv import load_dotenv
from tavily import TavilyClient

from research_artists import (
    OLLAMA_BASE_URL,
    DEFAULT_PARSER_MODEL,
    UNSUPPORTED_DOMAINS,
    AgentConfig,
    check_ollama,
    fetch_page,
    html_to_text,
    load_or_seed_venues,
    parse_concert_page,
    _is_unsupported_domain,
    _tavily_extract,
)

load_dotenv()

# 5 artists from the existing report (diverse URL sources) + Polyphia
TEST_CASES = [
    {
        "artist": "A Perfect Circle",
        "url": "https://detour.songkick.com/concerts/43058306-a-perfect-circle-at-halle-622",
        "source": "Songkick",
    },
    {
        "artist": "Altın Gün",
        "url": "https://www.ticketmaster.ch/event/altin-gun-tickets/884782846?language=en-us",
        "source": "Ticketmaster CH",
    },
    {
        "artist": "bar italia",
        "url": "https://www.dampfzentrale.ch/en/event/e-bar-italia_03-03-2026/",
        "source": "Dampfzentrale (venue site)",
    },
    {
        "artist": "Beirut",
        "url": "https://www.thehall.ch/en/events/beirut",
        "source": "The Hall (venue site)",
    },
    {
        "artist": "Blonde Redhead",
        "url": "https://www.instagram.com/p/DU0SiDnjNUJ/",
        "source": "Instagram",
    },
    {
        "artist": "Polyphia",
        "url": "https://www.songkick.com/artists/5765489-polyphia",
        "source": "Songkick artist page",
    },
]


def test_fetch_and_extract():
    """Test fetch_page + html_to_text on each URL."""
    print("=" * 70)
    print("PHASE 1: Fetch + HTML extraction (no LLM needed)")
    print("=" * 70)

    fetchable = []
    thin_content = []  # fetched OK but very little text extracted

    for case in TEST_CASES:
        artist = case["artist"]
        url = case["url"]
        source = case["source"]
        print(f"\n--- {artist} ({source}) ---")
        print(f"    URL: {url}")

        if _is_unsupported_domain(url):
            print("    ⊘ Skipped: unsupported domain (JS-only / auth required)")
            thin_content.append(case)
            continue

        try:
            html = fetch_page(url)
            text = html_to_text(html)
            print(f"    HTML size: {len(html):,} chars")
            print(f"    Extracted text: {len(text):,} chars")

            # Check for JSON-LD structured data
            if "[STRUCTURED DATA]" in text:
                ld_count = text.count("[STRUCTURED DATA]")
                print(f"    ✓ Found {ld_count} JSON-LD block(s)")

            if len(text.strip()) < 50:
                print(f"    ⚠ Very little text extracted — JS-heavy or blocked page")
                print(f"    Preview: {text[:200]!r}")
                thin_content.append(case)
            else:
                # Show a snippet around any date-like or venue-like content
                lines = text.split("\n")
                relevant = [l for l in lines if any(kw in l.lower() for kw in
                    ["2026", "zürich", "zurich", "ticket", "concert", "venue",
                     "halle", "hallenstadion", artist.lower().split()[0].lower()])]
                if relevant:
                    print(f"    Relevant lines ({len(relevant)} found):")
                    for line in relevant[:5]:
                        print(f"      > {line[:120]}")
                else:
                    print(f"    No obviously relevant lines found in text")
                    print(f"    First 300 chars: {text[:300]!r}")

            fetchable.append(case)

        except Exception as e:
            print(f"    ✗ Fetch failed: {e}")

    return fetchable, thin_content


def get_tavily_client() -> TavilyClient | None:
    """Return a TavilyClient if key is available, else None."""
    key = os.environ.get("TAVILY_API_KEY")
    if key:
        return TavilyClient(api_key=key)
    return None


def test_tavily_fallback(unfetchable: list, thin_content: list):
    """Test Tavily extract fallback on URLs that failed direct fetch or had thin content."""
    print("\n" + "=" * 70)
    print("PHASE 2: Tavily extract fallback")
    print("=" * 70)

    tavily = get_tavily_client()
    if not tavily:
        print("\n  ⊘ TAVILY_API_KEY not set. Skipping Tavily fallback tests.")
        return

    # Combine hard failures and thin-content pages (deduplicated)
    seen_urls = set()
    candidates = []
    for case in unfetchable + thin_content:
        if case["url"] not in seen_urls:
            seen_urls.add(case["url"])
            candidates.append(case)

    for case in candidates:
        artist = case["artist"]
        url = case["url"]
        source = case["source"]
        print(f"\n--- {artist} ({source}) ---")
        print(f"    URL: {url}")

        if _is_unsupported_domain(url):
            print("    ⊘ Skipped: unsupported domain (Tavily unlikely to help)")
            continue

        print("    Trying Tavily extract...")

        text = _tavily_extract(url, tavily)
        if text and len(text.strip()) >= 50:
            print(f"    ✓ Tavily extracted {len(text):,} chars")
            lines = text.split("\n")
            relevant = [l for l in lines if any(kw in l.lower() for kw in
                ["2026", "zürich", "zurich", "ticket", "concert",
                 artist.lower().split()[0].lower()])]
            if relevant:
                print(f"    Relevant lines ({len(relevant)} found):")
                for line in relevant[:5]:
                    print(f"      > {line[:120]}")
            else:
                print(f"    First 300 chars: {text[:300]!r}")
        else:
            print("    ✗ Tavily extract also failed or returned no content")


def test_ollama_extraction(tavily: TavilyClient | None):
    """Test full parse_concert_page with Ollama on all URLs (with Tavily fallback)."""
    print("\n" + "=" * 70)
    print("PHASE 3: Ollama extraction with Tavily fallback (all URLs)")
    print("=" * 70)

    ollama_available = check_ollama(OLLAMA_BASE_URL, DEFAULT_PARSER_MODEL)
    if not ollama_available:
        print(f"\n  ⊘ Ollama not running or {DEFAULT_PARSER_MODEL} not pulled. Skipping phase 3.")
        print(f"    To enable: ollama serve && ollama pull {DEFAULT_PARSER_MODEL}")
        return

    venues = load_or_seed_venues()
    print(f"    Venue map: {len(venues)} venues loaded")

    config = AgentConfig(
        mode="ollama",
        parser_client=openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=120.0),
        parser_model=DEFAULT_PARSER_MODEL,
        page_parser_enabled=True,
    )

    for case in TEST_CASES:
        artist = case["artist"]
        url = case["url"]
        source = case["source"]
        print(f"\n--- {artist} ({source}) ---")
        print(f"    Sending to {DEFAULT_PARSER_MODEL} (with Tavily fallback)...")

        result = parse_concert_page(url, artist, config, tavily, venues=venues)
        print(f"    Model response ({len(result)} chars):")
        for line in result.strip().split("\n"):
            print(f"      {line}")


if __name__ == "__main__":
    fetchable, thin_content = test_fetch_and_extract()

    print(f"\n── Fetch summary: {len(fetchable)}/{len(TEST_CASES)} URLs fetchable ──")
    unfetchable = [c for c in TEST_CASES if c not in fetchable and c not in thin_content]
    if unfetchable:
        print(f"   Could not fetch: {', '.join(c['artist'] for c in unfetchable)}")
    if thin_content:
        print(f"   Thin content / unsupported: {', '.join(c['artist'] for c in thin_content)}")

    test_tavily_fallback(unfetchable, thin_content)

    tavily = get_tavily_client()
    test_ollama_extraction(tavily)

    print("\n── Done ──")
