#!/usr/bin/env python3
"""
Concert Agent — main orchestrator.

Runs all pipeline steps in sequence:
  1. Sync favorite artists from Tidal
  2. Research activity status & find upcoming concerts
  3. Verify concert information
  4. Generate HTML reports

Toggle each step and tune parameters via the configuration section below.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

from tidal_client import get_session, get_favorite_artists

# ============================================================================
# CONFIGURATION — edit these variables to steer the pipeline
# ============================================================================

# --- Step toggles (set to False to skip a step) ---
SYNC_TIDAL = False           # Step 1: fetch favorite artists from Tidal
RESEARCH_ARTISTS = True     # Step 2: research activity & concerts
VERIFY_CONCERTS = True      # Step 3: verify found concerts
GENERATE_REPORT = True      # Step 4: generate HTML report (unverified)
GENERATE_VERIFIED_REPORT = True  # Step 4b: generate verified HTML report

# --- Research settings ---
RESEARCH_LOCAL = True      # Use Ollama instead of Anthropic API
RESEARCH_MAIN_MODEL = "qwen3:32b"   # Ollama main agent model
RESEARCH_PARSER_MODEL = "qwen3:8b"  # Ollama page parser model
RESEARCH_LIMIT = None       # Max artists to process (None = all)
RESEARCH_NAMES = None       # Comma-separated artist names to force-research (e.g. "Radiohead,Blur")
RESEARCH_NO_PAGE_PARSER = False  # Disable the page parser tool

# --- Verification settings ---
VERIFY_LIMIT = None         # Max concerts to verify (None = all)
VERIFY_SKIP_PAST = True     # Skip concerts with dates before today
VERIFY_NO_SEARCH = False    # Only Tier 1 (re-fetch), no Tavily search fallback
VERIFY_VERBOSE = False      # Enable DEBUG logging for verification

# --- File paths ---
ARTISTS_FILE = "favorite_artists.json"
CONCERTS_FILE = "upcoming_concerts.json"
VERIFIED_CONCERTS_FILE = "verified_concerts.json"
REPORT_FILE = "concert_report.html"
VERIFIED_REPORT_FILE = "concert_report_verified.html"

# ============================================================================
# END CONFIGURATION
# ============================================================================


def load_local_artists() -> dict:
    try:
        with open(ARTISTS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(a["id"]): a for a in data}
        return data
    except FileNotFoundError:
        return {}


def step_sync_tidal():
    """Step 1: Sync favorite artists from Tidal."""
    print("\n" + "=" * 60)
    print("STEP 1: Syncing favorite artists from Tidal")
    print("=" * 60)

    session = get_session()
    print(f"Logged in as: {session.user.first_name} {session.user.last_name}")

    local = load_local_artists()
    fresh = get_favorite_artists(session)
    print(f"Found {len(fresh)} favorite artists on Tidal.")

    now = datetime.now(timezone.utc).isoformat()
    for artist in fresh:
        key = str(artist["id"])
        entry = local.get(key, {})
        entry.update({
            "id": artist["id"],
            "name": artist["name"],
            "bio": artist["bio"],
            "last_updated": now,
        })
        local[key] = entry

    with open(ARTISTS_FILE, "w") as f:
        json.dump(local, f, indent=2)
    print(f"Saved {len(local)} artists to {ARTISTS_FILE}")


def step_research_artists():
    """Step 2: Research activity status and find upcoming concerts."""
    print("\n" + "=" * 60)
    print("STEP 2: Researching artists")
    print("=" * 60)

    cmd = [sys.executable, "research_artists.py"]
    if RESEARCH_LOCAL:
        cmd.append("--local")
        cmd.extend(["--main-model", RESEARCH_MAIN_MODEL])
        cmd.extend(["--parser-model", RESEARCH_PARSER_MODEL])
    if RESEARCH_LIMIT is not None:
        cmd.extend(["--limit", str(RESEARCH_LIMIT)])
    if RESEARCH_NAMES is not None:
        cmd.extend(["--names", RESEARCH_NAMES])
    if RESEARCH_NO_PAGE_PARSER:
        cmd.append("--no-page-parser")

    subprocess.run(cmd, check=True)


def step_verify_concerts():
    """Step 3: Verify concert information."""
    print("\n" + "=" * 60)
    print("STEP 3: Verifying concerts")
    print("=" * 60)

    cmd = [sys.executable, "verify_concerts.py"]
    cmd.extend(["--input", CONCERTS_FILE])
    cmd.extend(["--output", VERIFIED_CONCERTS_FILE])
    cmd.extend(["--report", VERIFIED_REPORT_FILE])
    cmd.extend(["--artists", ARTISTS_FILE])
    if VERIFY_LIMIT is not None:
        cmd.extend(["--limit", str(VERIFY_LIMIT)])
    if VERIFY_SKIP_PAST:
        cmd.append("--skip-past")
    if VERIFY_NO_SEARCH:
        cmd.append("--no-search")
    if VERIFY_VERBOSE:
        cmd.append("--verbose")

    subprocess.run(cmd, check=True)


def step_generate_report():
    """Step 4: Generate HTML report from unverified concerts."""
    print("\n" + "=" * 60)
    print("STEP 4: Generating concert report")
    print("=" * 60)

    cmd = [sys.executable, "generate_report.py"]
    cmd.extend(["--concerts", CONCERTS_FILE])
    cmd.extend(["--artists", ARTISTS_FILE])
    cmd.extend(["--output", REPORT_FILE])

    subprocess.run(cmd, check=True)


def main():
    print("Concert Agent Pipeline")
    print(f"Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if SYNC_TIDAL:
        step_sync_tidal()
    else:
        print("\nSkipping Step 1 (Tidal sync)")

    if RESEARCH_ARTISTS:
        step_research_artists()
    else:
        print("\nSkipping Step 2 (artist research)")

    if VERIFY_CONCERTS:
        step_verify_concerts()
    else:
        print("\nSkipping Step 3 (concert verification)")

    if GENERATE_REPORT:
        step_generate_report()
    else:
        print("\nSkipping Step 4 (report generation)")

    # Note: the verified report is already generated by verify_concerts.py
    # as part of Step 3, so no separate step is needed for GENERATE_VERIFIED_REPORT

    print("\n" + "=" * 60)
    print("Pipeline complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
