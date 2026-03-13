#!/usr/bin/env python3
"""
Verify concert information from upcoming_concerts.json.

Tiered verification strategy:
  Tier 1 — Re-fetch the original source URL and compare.
  Tier 2 — Targeted web search if source URL is unfetchable or inconclusive.
  Tier 3 — Mark as unverified if no signal from either tier.

Produces verified_concerts.json and concert_report_verified.html.
"""

import argparse
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tavily import TavilyClient
from tqdm import tqdm

from research_artists import (
    MAX_HTML_CHARS,
    UNSUPPORTED_DOMAINS,
    _get_session,
    _is_unsupported_domain,
    _tavily_extract,
    fetch_page,
    html_to_text,
    load_json,
    load_or_seed_venues,
    save_json,
)
from generate_report import (
    format_date_display,
    make_calendar_link,
    parse_date,
    sort_key,
    strip_tidal_markup,
    truncate,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("verify_concerts")

VERIFICATION_STATUSES = [
    "confirmed",
    "date_changed",
    "venue_changed",
    "details_changed",
    "cancelled",
    "past",
    "tentative",
    "festival_pending",
    "unverified",
]

# Domains known to block scraping or require JS.
_UNFETCHABLE_DOMAINS = UNSUPPORTED_DOMAINS | {
    "ticketmaster.ch",
    "shazam.com",
    "ra.co",
    "myswitzerland.com",
    "ticketcorner.ch",
    "concertful.com",
}

# Songkick artist pages return 410, but concert pages on detour.songkick.com work.
_SONGKICK_ARTIST_PATTERN = re.compile(r"songkick\.com/artists/")

# Known Swiss festivals — venue keywords that indicate a festival concert.
# Maps lowercase keyword → festival name for detection.
_FESTIVAL_KEYWORDS = {
    "openair st. gallen": "OpenAir St. Gallen",
    "openair st.gallen": "OpenAir St. Gallen",
    "open air st. gallen": "OpenAir St. Gallen",
    "festivalgelände sittertobel": "OpenAir St. Gallen",
    "musikfestwochen": "Winterthurer Musikfestwochen",
    "paléo": "Paléo Festival",
    "gurten": "Gurtenfestival",
    "montreux jazz": "Montreux Jazz Festival",
    "festi'neuch": "Festi'neuch",
    "festineuch": "Festi'neuch",
    "b-sides": "B-Sides Festival",
    "blue balls": "Blue Balls Festival",
    "jazz festival willisau": "Jazz Festival Willisau",
    "openair frauenfeld": "OpenAir Frauenfeld",
    "greenfield": "Greenfield Festival",
    "caribana": "Caribana Festival",
    "rock oz'arènes": "Rock Oz'Arènes",
}


# Venue name → event calendar URL for direct scraping.
# Only venues whose sites are known to be fetchable and list events on a single page.
_VENUE_CALENDARS = {
    "kaufleuten": "https://kaufleuten.ch/programm/",
    "the hall": "https://www.thehall.ch/events/",
    "the hall dübendorf": "https://www.thehall.ch/events/",
    "the hall zürich": "https://www.thehall.ch/events/",
    "docks": "https://www.docks.ch/programm/",
    "les docks": "https://www.docks.ch/programm/",
    "albani": "https://www.albani.ch/programm/",
    "komplex 457": "https://komplex457.ch/programm/",
    "komplex klub": "https://komplex457.ch/programm/",
    "x-tra": "https://www.x-tra.ch/programm/",
    "plaza": "https://www.x-tra.ch/programm/",
    "dampfzentrale": "https://www.dampfzentrale.ch/programm/",
    "stall 6": "https://www.stall6.ch/programm/",
    "bogen f": "https://bogenf.ch/programm/",
    "helsinki": "https://www.helsinkiklub.ch/events/",
    "exil": "https://www.exil.cl/programm/",
    "salzhaus": "https://salzhaus.ch/programm/",
    "bad bonn": "https://www.badbonn.ch/programm/",
    "fri-son": "https://fri-son.ch/programme/",
    "dachstock": "https://www.dachstock.ch/programm/",
    "bierhübeli": "https://www.bierhuebeli.ch/events/",
    "stadtkonzerte": "https://stadtkonzerte.ch/",
}


def _is_festival_concert(concert: dict) -> str | None:
    """Check if a concert is at a known festival.

    Returns the festival name if detected, else None.
    """
    venue = (concert.get("venue") or "").lower()
    for keyword, name in _FESTIVAL_KEYWORDS.items():
        if keyword in venue:
            return name
    return None


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


def classify_url(url: str) -> str:
    """Classify a URL as 'fetchable' or 'unfetchable'.

    Returns 'unfetchable' for domains that require JS rendering, auth,
    or are known to block automated fetches.
    """
    if _is_unsupported_domain(url):
        return "unfetchable"
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "unfetchable"
    # Check additional blocked domains
    if any(host == d or host.endswith("." + d) for d in _UNFETCHABLE_DOMAINS):
        return "unfetchable"
    # Songkick artist pages are 410, but concert pages work
    if _SONGKICK_ARTIST_PATTERN.search(url) and "detour." not in host:
        return "unfetchable"
    return "fetchable"


# ---------------------------------------------------------------------------
# Tier 1: Re-fetch source URL
# ---------------------------------------------------------------------------


def try_refetch_source(concert: dict, venues: dict) -> dict:
    """Attempt to re-fetch the source URL and extract text.

    Returns a log dict with fetch details and extracted text (or None).
    """
    url = concert.get("url", "")
    log = {
        "attempted": True,
        "url": url,
        "domain": "",
        "fetch_status": None,
        "http_status_code": None,
        "html_size_bytes": None,
        "extracted_text_size": None,
        "json_ld_found": False,
        "json_ld_types": [],
        "error_message": None,
        "redirect_chain": [],
        "response_headers": {},
        "duration_ms": None,
        "extracted_text": None,  # not persisted in log file, used in-memory
    }

    try:
        log["domain"] = urlparse(url).hostname or ""
    except Exception:
        log["domain"] = ""

    start = time.monotonic()
    try:
        session = _get_session()
        resp = session.get(url, timeout=15)

        log["http_status_code"] = resp.status_code
        log["response_headers"] = {
            "content-type": resp.headers.get("content-type", ""),
            "server": resp.headers.get("server", ""),
        }
        # Record redirect chain
        if resp.history:
            log["redirect_chain"] = [
                {"status": r.status_code, "url": r.url} for r in resp.history
            ]

        resp.raise_for_status()

        html = resp.text
        log["html_size_bytes"] = len(html.encode("utf-8", errors="replace"))

        # Extract text
        text = html_to_text(html, venues=venues)
        log["extracted_text_size"] = len(text)

        # Detect JSON-LD
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            content = tag.string
            if content and len(content.strip()) > 10:
                log["json_ld_found"] = True
                try:
                    data = json.loads(content.strip())
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        t = item.get("@type", "")
                        if t and t not in log["json_ld_types"]:
                            log["json_ld_types"].append(t)
                except (json.JSONDecodeError, AttributeError):
                    pass

        if len(text.strip()) < 50:
            log["fetch_status"] = "empty_response"
            log["error_message"] = (
                f"Page content too thin: {len(text.strip())} chars extracted "
                f"from {log['html_size_bytes']} bytes HTML"
            )
        else:
            log["fetch_status"] = "success"
            log["extracted_text"] = text

    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        log["duration_ms"] = elapsed
        err_type = type(e).__name__
        if "Timeout" in err_type:
            log["fetch_status"] = "timeout"
        elif "HTTPError" in err_type:
            log["fetch_status"] = "http_error"
        elif "ConnectionError" in err_type or "Connection" in err_type:
            log["fetch_status"] = "connection_error"
        else:
            log["fetch_status"] = "other_error"
        log["error_message"] = f"{err_type}: {e}"
        return log

    log["duration_ms"] = int((time.monotonic() - start) * 1000)
    return log


# ---------------------------------------------------------------------------
# Tier 1.5: Venue calendar check
# ---------------------------------------------------------------------------


def _get_venue_calendar_url(venue_name: str) -> str | None:
    """Look up a venue's event calendar URL from the known mapping."""
    if not venue_name:
        return None
    name_lower = venue_name.lower()
    # Try exact match first, then substring match
    if name_lower in _VENUE_CALENDARS:
        return _VENUE_CALENDARS[name_lower]
    for key, url in _VENUE_CALENDARS.items():
        if key in name_lower or name_lower in key:
            return url
    return None


def try_venue_calendar(concert: dict, venues: dict) -> dict:
    """Fetch the venue's event calendar and check if the artist appears.

    Returns a log dict with extracted text if the artist is found.
    """
    venue = concert.get("venue", "")
    artist = concert["artist_name"]
    cal_url = _get_venue_calendar_url(venue)

    log = {
        "attempted": False,
        "venue": venue,
        "calendar_url": cal_url,
        "artist_found": False,
        "extracted_text_size": None,
        "error_message": None,
        "duration_ms": None,
        "extracted_text": None,  # in-memory only
    }

    if not cal_url:
        return log

    log["attempted"] = True
    start = time.monotonic()
    try:
        session = _get_session()
        resp = session.get(cal_url, timeout=15)
        resp.raise_for_status()

        text = html_to_text(resp.text, venues=venues)
        log["extracted_text_size"] = len(text)

        # Check if the artist name appears in the calendar text
        if artist.lower() in text.lower():
            log["artist_found"] = True
            log["extracted_text"] = text
        else:
            log["error_message"] = f"Artist '{artist}' not found on venue calendar"
    except Exception as e:
        log["error_message"] = f"{type(e).__name__}: {e}"

    log["duration_ms"] = int((time.monotonic() - start) * 1000)
    return log


# ---------------------------------------------------------------------------
# Tier 2: Targeted web search
# ---------------------------------------------------------------------------


def _build_search_queries(concert: dict) -> list[str]:
    """Build a prioritized list of search queries for a concert.

    Returns up to 3 queries, tried in order until results are found:
      1. Artist + city + year on ticketing sites
      2. Artist + venue + date (original generic query)
      3. Artist + city only (broad fallback for niche artists)
    """
    artist = concert["artist_name"]
    venue = concert.get("venue", "")
    city = concert.get("city", "")
    date_str = concert.get("date", "")
    date_part = date_str[:7] if date_str and date_str != "TBD" else "2026"

    queries = []

    # Query 1: Site-targeted search on ticketing platforms
    city_part = city if city else "Switzerland"
    queries.append(
        f'"{artist}" {city_part} {date_part} '
        f"site:songkick.com OR site:bandsintown.com OR site:setlist.fm"
    )

    # Query 2: Original generic query with venue
    venue_part = f'"{venue}"' if venue and venue != "TBD" else ""
    queries.append(
        f'"{artist}" concert {venue_part} {date_part} Switzerland'.strip()
    )

    # Query 3: Broad fallback — artist + city only (helps niche artists)
    if city and city != "TBD":
        queries.append(f'"{artist}" concert "{city}" 2026')

    return queries


def search_for_concert(concert: dict, tavily: TavilyClient) -> dict:
    """Search for a concert using Tavily with multiple query strategies.

    Tries up to 3 queries in order, stopping at the first that returns results.
    """
    queries = _build_search_queries(concert)

    log = {
        "attempted": True,
        "reason_triggered": None,  # set by caller
        "query_used": None,
        "queries_tried": [],
        "num_results": None,
        "snippet_total_chars": None,
        "snippets_text": None,  # in-memory only
        "error_message": None,
        "duration_ms": None,
    }

    start = time.monotonic()
    try:
        for query in queries:
            log["queries_tried"].append(query)
            results = tavily.search(query, max_results=3)
            items = results.get("results", [])

            if items:
                log["query_used"] = query
                log["num_results"] = len(items)
                snippets = []
                for r in items:
                    title = r.get("title", "")
                    content = r.get("content", "")
                    url = r.get("url", "")
                    snippets.append(f"[{title}] {content} — {url}")
                combined = "\n\n".join(snippets)
                log["snippet_total_chars"] = len(combined)
                log["snippets_text"] = combined
                break

            # Brief pause between queries to respect rate limits
            time.sleep(0.5)
        else:
            log["query_used"] = queries[-1]
            log["num_results"] = 0
            log["error_message"] = (
                f"No results from any of {len(queries)} queries"
            )
    except Exception as e:
        log["error_message"] = f"{type(e).__name__}: {e}"

    log["duration_ms"] = int((time.monotonic() - start) * 1000)
    return log


# ---------------------------------------------------------------------------
# LLM comparison
# ---------------------------------------------------------------------------

_COMPARISON_PROMPT = """You are verifying a concert listing. Compare the stored concert record against the evidence text and determine if the concert information is still accurate.

STORED CONCERT RECORD:
- Artist: {artist}
- Date: {date}
- Venue: {venue}
- City: {city}
- Country: {country}
- Source URL: {url}

EVIDENCE TEXT:
{evidence}

INSTRUCTIONS:
- If the evidence contains clear confirmation of the same artist, date, and venue: status = "confirmed"
- If the evidence shows the concert exists but the DATE has changed: status = "date_changed" (include new_date)
- If the evidence shows the concert exists but the VENUE or CITY has changed: status = "venue_changed" (include new_venue and/or new_city)
- If multiple fields changed: status = "details_changed" (include all changed fields)
- If the evidence explicitly says the concert is cancelled or removed: status = "cancelled"
- If the evidence contains NO mention of this artist or event at this venue: status = "unverified"
- Confidence: "high" if directly stated, "medium" if inferred, "low" if weak signal

Respond with ONLY this JSON (no markdown, no extra text):
{{"status": "confirmed|date_changed|venue_changed|details_changed|cancelled|unverified", "confidence": "high|medium|low", "new_date": null, "new_venue": null, "new_city": null, "notes": "Brief 1-sentence explanation"}}"""


def compare_concert_info(
    concert: dict, evidence_text: str, client: anthropic.Anthropic
) -> dict:
    """Use Claude Sonnet to compare stored concert info vs evidence text.

    Returns a log dict with LLM response details and parsed verdict.
    """
    prompt = _COMPARISON_PROMPT.format(
        artist=concert["artist_name"],
        date=concert.get("date", "TBD"),
        venue=concert.get("venue", "TBD"),
        city=concert.get("city", ""),
        country=concert.get("country", "Switzerland"),
        url=concert.get("url", ""),
        evidence=evidence_text[:8000],  # cap evidence length
    )

    log = {
        "attempted": True,
        "evidence_source": None,  # set by caller
        "evidence_chars": len(evidence_text),
        "model": "claude-sonnet-4-6",
        "input_tokens": None,
        "output_tokens": None,
        "raw_response": None,
        "parse_success": False,
        "parsed_result": None,
        "error_message": None,
        "duration_ms": None,
    }

    start = time.monotonic()
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )

        log["input_tokens"] = response.usage.input_tokens
        log["output_tokens"] = response.usage.output_tokens

        raw_text = response.content[0].text if response.content else ""
        log["raw_response"] = raw_text

        # Parse JSON from response
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            log["parse_success"] = True
            log["parsed_result"] = parsed
        else:
            log["error_message"] = "No JSON object found in LLM response"

    except json.JSONDecodeError as e:
        log["error_message"] = f"JSON parse error: {e}. Raw: {raw_text[:200]}"
    except Exception as e:
        log["error_message"] = f"{type(e).__name__}: {e}"

    log["duration_ms"] = int((time.monotonic() - start) * 1000)
    return log


# ---------------------------------------------------------------------------
# Single concert verification
# ---------------------------------------------------------------------------


def verify_single_concert(
    concert: dict,
    index: int,
    total: int,
    tavily: TavilyClient,
    client: anthropic.Anthropic,
    venues: dict,
    no_search: bool = False,
) -> tuple[dict, dict]:
    """Verify one concert entry. Returns (verified_concert, log_entry)."""

    concert_log = {
        "artist_name": concert["artist_name"],
        "concert_index": index,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url_classification": None,
        "tier1": {
            "attempted": False,
            "url": concert.get("url", ""),
            "domain": "",
            "fetch_status": None,
            "http_status_code": None,
            "html_size_bytes": None,
            "extracted_text_size": None,
            "json_ld_found": False,
            "json_ld_types": [],
            "error_message": None,
            "redirect_chain": [],
            "response_headers": {},
            "duration_ms": None,
        },
        "tier2": {
            "attempted": False,
            "reason_triggered": None,
            "query_used": None,
            "num_results": None,
            "snippet_total_chars": None,
            "error_message": None,
            "duration_ms": None,
        },
        "llm_comparison": {
            "attempted": False,
            "evidence_source": None,
            "evidence_chars": None,
            "model": "claude-sonnet-4-6",
            "input_tokens": None,
            "output_tokens": None,
            "raw_response": None,
            "parse_success": False,
            "error_message": None,
            "duration_ms": None,
        },
        "result": {
            "status": "unverified",
            "confidence": "low",
            "changes": {},
            "notes": "",
        },
    }

    start_total = time.monotonic()

    # --- Check if past ---
    date_str = concert.get("date", "TBD")
    today = date.today()
    dt = parse_date(date_str)
    if dt is not None and dt < today:
        concert_log["result"] = {
            "status": "past",
            "confidence": "high",
            "changes": {},
            "notes": f"Concert date {date_str} is in the past",
        }
        verified = {**concert, "verification": concert_log["result"]}
        verified["verification"]["method"] = "date_check"
        verified["verification"]["verified_at"] = datetime.now(timezone.utc).isoformat()
        _log_concert_line(index, total, concert, concert_log, start_total)
        return verified, concert_log

    # --- Classify URL ---
    url_class = classify_url(concert.get("url", ""))
    concert_log["url_classification"] = url_class

    evidence_text = None
    evidence_source = None

    # --- Tier 1: Re-fetch source URL ---
    if url_class == "fetchable":
        tier1_log = try_refetch_source(concert, venues)
        # Merge into concert_log, keeping extracted_text separate
        extracted = tier1_log.pop("extracted_text", None)
        concert_log["tier1"] = tier1_log

        if extracted:
            evidence_text = extracted
            evidence_source = "source_refetch"
            logger.debug(
                "Tier1 OK: %s — %d chars from %s",
                concert["artist_name"],
                len(extracted),
                tier1_log["domain"],
            )
        else:
            logger.debug(
                "Tier1 FAIL: %s — %s: %s",
                concert["artist_name"],
                tier1_log["fetch_status"],
                tier1_log.get("error_message", ""),
            )
    else:
        try:
            concert_log["tier1"]["domain"] = urlparse(concert.get("url", "")).hostname or ""
        except Exception:
            pass
        concert_log["tier1"]["attempted"] = False
        concert_log["tier1"]["error_message"] = f"URL classified as {url_class}"
        logger.debug(
            "Tier1 SKIP: %s — %s (%s)",
            concert["artist_name"],
            url_class,
            concert.get("url", "")[:60],
        )

    # --- Tier 1.5: Venue calendar check (if tier1 yielded no evidence) ---
    if evidence_text is None:
        vcal_log = try_venue_calendar(concert, venues)
        concert_log["venue_calendar"] = {
            k: v for k, v in vcal_log.items() if k != "extracted_text"
        }
        vcal_text = vcal_log.get("extracted_text")
        if vcal_text:
            evidence_text = vcal_text
            evidence_source = "venue_calendar"
            logger.debug(
                "Tier1.5 OK: %s — found on %s calendar (%d chars)",
                concert["artist_name"],
                vcal_log["venue"],
                len(vcal_text),
            )
        elif vcal_log["attempted"]:
            logger.debug(
                "Tier1.5 FAIL: %s — %s",
                concert["artist_name"],
                vcal_log.get("error_message", "not found"),
            )

    # --- Helper: run tier2 search ---
    def _run_tier2(reason: str) -> str | None:
        """Run a Tavily search and return snippets text, or None."""
        tier2_log = search_for_concert(concert, tavily)
        snippets = tier2_log.pop("snippets_text", None)
        tier2_log["reason_triggered"] = reason
        concert_log["tier2"] = tier2_log

        if snippets:
            logger.debug(
                "Tier2 OK: %s — %d results, %d chars",
                concert["artist_name"],
                tier2_log["num_results"],
                len(snippets),
            )
        else:
            logger.debug(
                "Tier2 FAIL: %s — %s",
                concert["artist_name"],
                tier2_log.get("error_message", "no results"),
            )
        # Rate limit for Tavily
        time.sleep(1.0)
        return snippets

    # --- Helper: run LLM comparison and extract result ---
    def _run_llm(ev_text: str, ev_source: str) -> dict:
        """Run LLM comparison and return parsed result dict."""
        llm_log = compare_concert_info(concert, ev_text, client)
        llm_log["evidence_source"] = ev_source
        parsed = llm_log.pop("parsed_result", None)
        concert_log["llm_comparison"] = llm_log

        if parsed and llm_log["parse_success"]:
            status = parsed.get("status", "unverified")
            confidence = parsed.get("confidence", "low")
            notes = parsed.get("notes", "")
            changes = {}
            if parsed.get("new_date"):
                changes["new_date"] = parsed["new_date"]
            if parsed.get("new_venue"):
                changes["new_venue"] = parsed["new_venue"]
            if parsed.get("new_city"):
                changes["new_city"] = parsed["new_city"]

            # If the LLM reports a date change to a past date, override to "past"
            if status == "date_changed" and changes.get("new_date"):
                new_dt = parse_date(changes["new_date"])
                if new_dt is not None and new_dt < today:
                    status = "past"
                    confidence = "high"
                    notes = (f"LLM reported date_changed to {changes['new_date']} "
                             f"but that date is in the past")

            return {
                "status": status,
                "confidence": confidence,
                "changes": changes,
                "notes": notes,
            }
        else:
            return {
                "status": "unverified",
                "confidence": "low",
                "changes": {},
                "notes": f"LLM comparison failed: {llm_log.get('error_message', 'unknown')}",
            }

    # --- Tier 2: Web search (if tier1 failed or was skipped) ---
    if evidence_text is None and not no_search:
        reason = "url_unfetchable" if url_class == "unfetchable" else "tier1_failed"
        snippets = _run_tier2(reason)
        if snippets:
            evidence_text = snippets
            evidence_source = "web_search"

    # --- LLM comparison ---
    if evidence_text:
        result = _run_llm(evidence_text, evidence_source)
        concert_log["result"] = result

        # --- Tier 2 fallback: if tier1 succeeded but LLM said unverified,
        #     try a supplemental web search for a second opinion ---
        if (
            result["status"] == "unverified"
            and evidence_source == "source_refetch"
            and not no_search
        ):
            logger.debug(
                "Tier2 FALLBACK: %s — tier1 inconclusive, trying web search",
                concert["artist_name"],
            )
            snippets = _run_tier2("tier1_inconclusive")
            if snippets:
                # Combine tier1 + tier2 evidence for a richer context
                combined = evidence_text + "\n\n--- WEB SEARCH RESULTS ---\n\n" + snippets
                result = _run_llm(combined, "source_refetch+web_search")
                concert_log["result"] = result
    else:
        concert_log["result"] = {
            "status": "unverified",
            "confidence": "low",
            "changes": {},
            "notes": "No evidence obtained from either tier",
        }

    # --- Override unverified → festival_pending for festival concerts ---
    if concert_log["result"]["status"] == "unverified":
        festival_name = _is_festival_concert(concert)
        if festival_name:
            # Check if the festival is >2 months away (lineup may not be published yet)
            concert_dt = parse_date(concert.get("date", "TBD"))
            if concert_dt is not None and (concert_dt - today).days > 60:
                concert_log["result"]["status"] = "festival_pending"
                concert_log["result"]["notes"] = (
                    f"Festival ({festival_name}) confirmed but per-artist lineup "
                    f"may not be published yet. "
                    + concert_log["result"].get("notes", "")
                ).strip()

    # --- Override unverified → tentative for social-media-only sources ---
    if (
        concert_log["result"]["status"] == "unverified"
        and concert.get("source_quality") == "social_media_only"
    ):
        concert_log["result"]["status"] = "tentative"
        concert_log["result"]["notes"] = (
            "Source was social media only; no corroborating evidence found. "
            + concert_log["result"].get("notes", "")
        ).strip()

    # --- Build verified concert entry ---
    verified = {**concert}
    verified["verification"] = {
        **concert_log["result"],
        "method": evidence_source or "none",
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }

    _log_concert_line(index, total, concert, concert_log, start_total)
    return verified, concert_log


def _log_concert_line(
    index: int, total: int, concert: dict, log: dict, start_total: float
) -> None:
    """Print a concise one-line progress summary."""
    elapsed = time.monotonic() - start_total
    artist = concert["artist_name"]
    result = log["result"]
    status = result["status"]
    confidence = result["confidence"]

    parts = [f"[{index + 1}/{total}] {artist}"]

    # Tier 1 status
    t1 = log["tier1"]
    if result["status"] == "past":
        parts.append("PAST")
    elif not t1["attempted"]:
        parts.append(f"Tier1:SKIP({log.get('url_classification', '?')})")
    elif t1["fetch_status"] == "success":
        parts.append(f"Tier1:OK fetch={t1['duration_ms']}ms")
    else:
        parts.append(f"Tier1:FAIL({t1['fetch_status']})")

    # Tier 2 status
    t2 = log["tier2"]
    if t2["attempted"]:
        if t2["num_results"] and t2["num_results"] > 0:
            parts.append(f"Tier2:OK {t2['num_results']} results")
        else:
            parts.append(f"Tier2:FAIL({t2.get('error_message', 'no results')[:30]})")

    # Result
    parts.append(f"=> {status}({confidence})")
    parts.append(f"{elapsed:.1f}s")

    tqdm.write(" — ".join(parts))


# ---------------------------------------------------------------------------
# Verify all concerts
# ---------------------------------------------------------------------------


def verify_all_concerts(
    concerts: list,
    tavily: TavilyClient,
    client: anthropic.Anthropic,
    venues: dict,
    limit: int | None = None,
    no_search: bool = False,
) -> tuple[list, list]:
    """Verify all concerts. Returns (verified_concerts, logs)."""
    to_verify = concerts[:limit] if limit else concerts
    total = len(to_verify)

    verified_list = []
    all_logs = []

    pbar = tqdm(enumerate(to_verify), total=total, desc="Verifying concerts", unit="concert")
    for i, concert in pbar:
        pbar.set_postfix_str(concert["artist_name"][:25])
        verified, log_entry = verify_single_concert(
            concert, i, total, tavily, client, venues, no_search=no_search
        )
        verified_list.append(verified)
        all_logs.append(log_entry)

    # Append unprocessed concerts (beyond limit) as-is with unverified status
    if limit and limit < len(concerts):
        for concert in concerts[limit:]:
            verified = {
                **concert,
                "verification": {
                    "status": "unverified",
                    "confidence": "low",
                    "method": "skipped",
                    "changes": {},
                    "notes": "Not processed (beyond --limit)",
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            verified_list.append(verified)

    return verified_list, all_logs


# ---------------------------------------------------------------------------
# End-of-run summary
# ---------------------------------------------------------------------------


def print_summary(logs: list, all_concerts: list) -> dict:
    """Print and return an end-of-run summary."""
    status_counts = Counter()
    tier1_domain_stats = defaultdict(lambda: {"success": 0, "fail": 0})
    tier2_reasons = Counter()
    tier1_times = []
    tier2_times = []
    llm_times = []
    total_tavily_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0

    for log in logs:
        status_counts[log["result"]["status"]] += 1

        t1 = log["tier1"]
        if t1["attempted"]:
            domain = t1.get("domain", "unknown")
            if t1["fetch_status"] == "success":
                tier1_domain_stats[domain]["success"] += 1
            else:
                tier1_domain_stats[domain]["fail"] += 1
            if t1["duration_ms"] is not None:
                tier1_times.append(t1["duration_ms"])

        t2 = log["tier2"]
        if t2["attempted"]:
            total_tavily_calls += 1
            if t2["reason_triggered"]:
                tier2_reasons[t2["reason_triggered"]] += 1
            if t2["duration_ms"] is not None:
                tier2_times.append(t2["duration_ms"])

        llm = log["llm_comparison"]
        if llm["attempted"]:
            if llm["input_tokens"]:
                total_input_tokens += llm["input_tokens"]
            if llm["output_tokens"]:
                total_output_tokens += llm["output_tokens"]
            if llm["duration_ms"] is not None:
                llm_times.append(llm["duration_ms"])

    # Print summary
    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)

    print(f"\nTotal concerts processed: {len(logs)}")
    print(f"Total concerts in output: {len(all_concerts)}")

    print("\nStatus breakdown:")
    for status in VERIFICATION_STATUSES:
        count = status_counts.get(status, 0)
        if count > 0:
            print(f"  {status:20s}: {count}")

    print("\nTier 1 (source re-fetch) — domain success rates:")
    # Sort by total attempts descending
    for domain, stats in sorted(
        tier1_domain_stats.items(),
        key=lambda x: x[1]["success"] + x[1]["fail"],
        reverse=True,
    ):
        total = stats["success"] + stats["fail"]
        rate = stats["success"] / total * 100 if total > 0 else 0
        marker = " ** CANDIDATE FOR UNSUPPORTED" if rate < 50 and total >= 2 else ""
        print(f"  {domain:40s}: {stats['success']}/{total} ({rate:.0f}%){marker}")

    if tier1_times:
        avg_t1 = sum(tier1_times) / len(tier1_times)
        print(f"\n  Avg Tier 1 fetch time: {avg_t1:.0f}ms")

    print(f"\nTier 2 (web search) — {total_tavily_calls} Tavily API calls")
    if tier2_reasons:
        print("  Trigger reasons:")
        for reason, count in tier2_reasons.most_common():
            print(f"    {reason}: {count}")
    if tier2_times:
        avg_t2 = sum(tier2_times) / len(tier2_times)
        print(f"  Avg Tier 2 search time: {avg_t2:.0f}ms")

    print(f"\nLLM comparisons: {len(llm_times)} calls")
    print(f"  Total input tokens:  {total_input_tokens:,}")
    print(f"  Total output tokens: {total_output_tokens:,}")
    # Rough cost estimate for Sonnet: $3/M input, $15/M output
    cost = (total_input_tokens * 3 + total_output_tokens * 15) / 1_000_000
    print(f"  Estimated cost:      ${cost:.4f}")
    if llm_times:
        avg_llm = sum(llm_times) / len(llm_times)
        print(f"  Avg LLM call time:   {avg_llm:.0f}ms")

    # Domains that fail >50%
    failing_domains = [
        d
        for d, s in tier1_domain_stats.items()
        if s["success"] + s["fail"] >= 2
        and s["success"] / (s["success"] + s["fail"]) < 0.5
    ]
    if failing_domains:
        print("\nDomains failing >50% (candidates for UNSUPPORTED_DOMAINS):")
        for d in failing_domains:
            print(f"  - {d}")

    print("=" * 60)

    return {
        "status_counts": dict(status_counts),
        "tier1_domain_stats": {k: dict(v) for k, v in tier1_domain_stats.items()},
        "tier2_reasons": dict(tier2_reasons),
        "total_tavily_calls": total_tavily_calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "estimated_cost_usd": round(cost, 4),
        "avg_tier1_ms": round(sum(tier1_times) / len(tier1_times)) if tier1_times else None,
        "avg_tier2_ms": round(sum(tier2_times) / len(tier2_times)) if tier2_times else None,
        "avg_llm_ms": round(sum(llm_times) / len(llm_times)) if llm_times else None,
        "failing_domains": failing_domains,
    }


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------

_BADGE_STYLES = {
    "confirmed": ("Verified", "badge-confirmed"),
    "date_changed": ("Date Changed", "badge-changed"),
    "venue_changed": ("Venue Changed", "badge-changed"),
    "details_changed": ("Details Changed", "badge-changed"),
    "cancelled": ("Cancelled", "badge-cancelled"),
    "past": ("Past Event", "badge-past"),
    "tentative": ("Social Media Only", "badge-tentative"),
    "festival_pending": ("Lineup Pending", "badge-tentative"),
    "unverified": ("Unverified", "badge-unverified"),
}


def generate_verified_report(
    concerts: list, artist_bios: dict, output_path: Path
) -> None:
    """Generate an HTML report with verification badges."""
    sorted_concerts = sorted(concerts, key=sort_key)
    generated = datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    total = len(sorted_concerts)

    # Count statuses for header
    status_counts = Counter()
    for c in sorted_concerts:
        v = c.get("verification", {})
        status_counts[v.get("status", "unverified")] += 1

    status_summary_parts = []
    for status, label in [
        ("confirmed", "Verified"),
        ("date_changed", "Date Changed"),
        ("venue_changed", "Venue Changed"),
        ("details_changed", "Details Changed"),
        ("cancelled", "Cancelled"),
        ("past", "Past"),
        ("tentative", "Social Media Only"),
        ("festival_pending", "Lineup Pending"),
        ("unverified", "Unverified"),
    ]:
        count = status_counts.get(status, 0)
        if count > 0:
            status_summary_parts.append(f"{label}: {count}")
    status_summary = " &nbsp;|&nbsp; ".join(status_summary_parts)

    cards_html = []
    prev_tbd = False

    for c in sorted_concerts:
        is_tbd = c["date"] == "TBD" or parse_date(c["date"]) is None
        verification = c.get("verification", {})
        v_status = verification.get("status", "unverified")
        v_confidence = verification.get("confidence", "low")
        v_notes = verification.get("notes", "")
        v_changes = verification.get("changes", {})

        if is_tbd and not prev_tbd:
            cards_html.append(
                '<div class="separator"><span>Date Unknown</span></div>'
            )
            prev_tbd = True

        artist_id = str(c["artist_id"])
        raw_bio = artist_bios.get(artist_id, {}).get("bio", "")
        bio = truncate(strip_tidal_markup(raw_bio)) if raw_bio else ""

        date_display = format_date_display(c["date"])
        venue_line = f"{c['venue']}, {c['city']}"

        # Verification badge
        badge_label, badge_class = _BADGE_STYLES.get(
            v_status, ("Unknown", "badge-unverified")
        )
        # Add confidence indicator for confirmed
        if v_status == "confirmed" and v_confidence in ("medium", "low"):
            badge_label = "Likely OK"
            badge_class = "badge-likely"

        # Enhance badge label with change details
        if v_status == "date_changed" and v_changes.get("new_date"):
            badge_label = f"Date: {v_changes['new_date']}"
        elif v_status == "venue_changed" and v_changes.get("new_venue"):
            badge_label = f"Venue: {v_changes['new_venue']}"

        badge_html = f'<span class="v-badge {badge_class}">{badge_label}</span>'

        cal_button = ""
        if not is_tbd and v_status != "cancelled":
            cal_href = make_calendar_link(c, bio)
            safe_name = re.sub(r"[^\w\-]", "_", c["artist_name"])
            cal_button = (
                f'<a class="btn btn-cal" href="{cal_href}" '
                f'download="{safe_name}.ics">Add to Calendar</a>'
            )

        source_button = (
            f'<a class="btn btn-src" href="{c["url"]}" '
            f'target="_blank" rel="noopener">Source</a>'
        )

        bio_html = (
            f'<p class="bio">{bio}</p>'
            if bio
            else '<p class="bio no-bio">No bio available</p>'
        )

        notes_html = ""
        if v_notes:
            notes_html = f'<p class="v-notes">{v_notes}</p>'

        # Card CSS class modifiers
        card_classes = ["card"]
        if is_tbd:
            card_classes.append("tbd")
        if v_status == "cancelled":
            card_classes.append("cancelled")
        if v_status == "past":
            card_classes.append("past-event")

        card = f"""
        <div class="{' '.join(card_classes)}">
          <div class="card-header">
            <span class="artist">{c['artist_name']}</span>
            <div class="badges">
              <span class="date-badge">{date_display}</span>
              {badge_html}
            </div>
          </div>
          <div class="venue">{venue_line}</div>
          {notes_html}
          {bio_html}
          <div class="actions">
            {source_button}
            {cal_button}
          </div>
        </div>"""
        cards_html.append(card)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Verified Concert Report</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 0;
      background: #111;
      color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 15px;
      line-height: 1.5;
    }}
    header {{
      background: #1a1a2e;
      border-bottom: 2px solid #7c4dff;
      padding: 24px 32px;
    }}
    header h1 {{
      margin: 0 0 4px;
      font-size: 2em;
      color: #fff;
      letter-spacing: -0.5px;
    }}
    header .meta {{
      color: #888;
      font-size: 0.85em;
    }}
    header .status-summary {{
      color: #aaa;
      font-size: 0.82em;
      margin-top: 6px;
    }}
    .container {{
      max-width: 820px;
      margin: 32px auto;
      padding: 0 16px;
    }}
    .card {{
      background: #1e1e1e;
      border: 1px solid #2a2a2a;
      border-radius: 10px;
      padding: 20px 22px;
      margin-bottom: 16px;
      transition: border-color 0.15s;
    }}
    .card:hover {{
      border-color: #7c4dff;
    }}
    .card.tbd {{
      opacity: 0.75;
      border-style: dashed;
    }}
    .card.cancelled {{
      opacity: 0.5;
      border-color: #ff4444;
    }}
    .card.cancelled .artist {{
      text-decoration: line-through;
    }}
    .card.past-event {{
      opacity: 0.45;
    }}
    .card-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }}
    .badges {{
      display: flex;
      gap: 6px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .artist {{
      font-size: 1.2em;
      font-weight: 700;
      color: #fff;
    }}
    .date-badge {{
      background: #7c4dff22;
      border: 1px solid #7c4dff66;
      color: #b39dff;
      border-radius: 6px;
      padding: 2px 10px;
      font-size: 0.82em;
      white-space: nowrap;
    }}
    .v-badge {{
      border-radius: 6px;
      padding: 2px 10px;
      font-size: 0.75em;
      font-weight: 600;
      white-space: nowrap;
    }}
    .badge-confirmed {{
      background: #1b5e2022;
      border: 1px solid #4caf5066;
      color: #81c784;
    }}
    .badge-likely {{
      background: #33691e22;
      border: 1px solid #8bc34a66;
      color: #aed581;
    }}
    .badge-changed {{
      background: #e6510022;
      border: 1px solid #ff980066;
      color: #ffb74d;
    }}
    .badge-cancelled {{
      background: #b7121222;
      border: 1px solid #ff444466;
      color: #ef5350;
    }}
    .badge-past {{
      background: #42424222;
      border: 1px solid #75757566;
      color: #9e9e9e;
    }}
    .badge-tentative {{
      background: #f57f1722;
      border: 1px solid #ff980066;
      color: #ffb74d;
    }}
    .badge-unverified {{
      background: #42424222;
      border: 1px dashed #75757566;
      color: #9e9e9e;
    }}
    .venue {{
      color: #aaa;
      font-size: 0.9em;
      margin-bottom: 4px;
    }}
    .v-notes {{
      color: #888;
      font-size: 0.78em;
      font-style: italic;
      margin: 2px 0 8px;
    }}
    .bio {{
      color: #c0c0c0;
      font-size: 0.88em;
      margin: 0 0 14px;
    }}
    .bio.no-bio {{
      color: #555;
      font-style: italic;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .btn {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.82em;
      font-weight: 500;
      text-decoration: none;
      cursor: pointer;
      transition: opacity 0.15s;
    }}
    .btn:hover {{ opacity: 0.8; }}
    .btn-src {{
      background: #2a2a3e;
      border: 1px solid #444;
      color: #ccc;
    }}
    .btn-cal {{
      background: #7c4dff22;
      border: 1px solid #7c4dff66;
      color: #b39dff;
    }}
    .separator {{
      text-align: center;
      margin: 28px 0 20px;
      position: relative;
    }}
    .separator::before {{
      content: "";
      position: absolute;
      top: 50%;
      left: 0;
      right: 0;
      height: 1px;
      background: #333;
    }}
    .separator span {{
      position: relative;
      background: #111;
      padding: 0 14px;
      color: #666;
      font-size: 0.8em;
      text-transform: uppercase;
      letter-spacing: 1px;
    }}
    footer {{
      text-align: center;
      color: #444;
      font-size: 0.8em;
      padding: 32px 16px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Verified Concert Report</h1>
    <div class="meta">Generated {generated} &nbsp;&middot;&nbsp; {total} concerts</div>
    <div class="status-summary">{status_summary}</div>
  </header>
  <div class="container">
    {''.join(cards_html)}
  </div>
  <footer>Generated by concert_agent — verification pass</footer>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"Verified report written to {output_path}  ({total} concerts)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Verify concert information and generate a verified report."
    )
    parser.add_argument(
        "--input",
        default="upcoming_concerts.json",
        help="Input concert JSON (default: upcoming_concerts.json)",
    )
    parser.add_argument(
        "--output",
        default="verified_concerts.json",
        help="Output verified concert JSON (default: verified_concerts.json)",
    )
    parser.add_argument(
        "--report",
        default="concert_report_verified.html",
        help="Output HTML report (default: concert_report_verified.html)",
    )
    parser.add_argument(
        "--artists",
        default="favorite_artists.json",
        help="Path to favorite_artists.json for bios (default: favorite_artists.json)",
    )
    parser.add_argument(
        "--log",
        default="verification_log.json",
        help="Path to structured JSON log file (default: verification_log.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only verify the first N concerts (for testing)",
    )
    parser.add_argument(
        "--skip-past",
        action="store_true",
        help="Skip concerts with dates before today",
    )
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="Only do Tier 1 (re-fetch), never fall back to Tavily search",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set logging to DEBUG level",
    )
    args = parser.parse_args()

    # Logging setup
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv()

    # API clients
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise SystemExit("ANTHROPIC_API_KEY not set in environment / .env")
    client = anthropic.Anthropic(api_key=anthropic_key)

    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        raise SystemExit("TAVILY_API_KEY not set in environment / .env")
    tavily = TavilyClient(api_key=tavily_key)

    # Load data
    concerts_path = Path(args.input)
    if not concerts_path.exists():
        raise SystemExit(f"Input file not found: {concerts_path}")
    concerts = json.loads(concerts_path.read_text(encoding="utf-8"))

    venues = load_or_seed_venues()
    print(f"Loaded {len(concerts)} concerts, {len(venues)} venues")

    # Optionally filter out past concerts
    if args.skip_past:
        today = date.today()
        original_count = len(concerts)
        past = []
        future = []
        for c in concerts:
            dt = parse_date(c.get("date", "TBD"))
            if dt is not None and dt < today:
                past.append(c)
            else:
                future.append(c)
        concerts = future
        print(f"Skipped {len(past)} past concerts ({len(concerts)} remaining)")

    # Verify
    verified_concerts, logs = verify_all_concerts(
        concerts, tavily, client, venues,
        limit=args.limit,
        no_search=args.no_search,
    )

    # Summary
    summary = print_summary(logs, verified_concerts)

    # Save outputs
    save_json(args.output, verified_concerts)
    print(f"\nSaved {len(verified_concerts)} verified concerts to {args.output}")

    # Save log
    log_data = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "input_file": args.input,
        "total_concerts": len(concerts),
        "limit": args.limit,
        "no_search": args.no_search,
        "skip_past": args.skip_past,
        "summary": summary,
        "entries": logs,
    }
    save_json(args.log, log_data)
    print(f"Saved verification log to {args.log}")

    # Generate HTML report
    artists_path = Path(args.artists)
    artist_bios = {}
    if artists_path.exists():
        artist_bios = json.loads(artists_path.read_text(encoding="utf-8"))
    else:
        print(f"Warning: artists file not found ({artists_path}), bios will be omitted")

    generate_verified_report(verified_concerts, artist_bios, Path(args.report))


if __name__ == "__main__":
    main()
