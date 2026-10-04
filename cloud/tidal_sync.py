"""
Sync Tidal favorite artists into cloud/data/favorite_artists.json.

Cloud-friendly variant of ../tidal_client.py: no interactive login, no session
file. Authenticates with the refresh token in the TIDAL_REFRESH_TOKEN
environment variable (the `refresh_token` field of a local tidal_session.json).

Tidal-sourced fields (name) are refreshed; every other field on an artist
(research status, notes, ...) is preserved. Artists that were unfavorited are
kept but flagged with "favorite": false so their history survives.

Usage:
    uv run python cloud/tidal_sync.py            # sync, no bios
    uv run python cloud/tidal_sync.py --bios     # also fetch bios for artists missing one
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import tidalapi

ARTISTS_FILE = Path(__file__).parent / "data" / "favorite_artists.json"


def get_session() -> tidalapi.Session:
    refresh_token = os.environ.get("TIDAL_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        sys.exit("TIDAL_REFRESH_TOKEN is not set; add it to the cloud environment.")
    session = tidalapi.Session()
    try:
        ok = session.token_refresh(refresh_token)
    except Exception as e:  # tidalapi raises AuthenticationError on a revoked token
        sys.exit(f"Tidal token refresh failed ({e}). A new Tidal login is needed.")
    if not ok or not session.load_oauth_session(
        session.token_type, session.access_token, refresh_token, session.expiry_time
    ):
        sys.exit("Tidal login failed after token refresh. A new Tidal login is needed.")
    return session


def clean_bio(bio: str | None) -> str | None:
    if not bio:
        return None
    return re.sub(r"\[/?wimpLink[^\]]*\]", "", bio).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--bios", action="store_true", help="fetch bios for artists without one")
    args = parser.parse_args()

    session = get_session()
    favorites = session.user.favorites.artists_paginated()

    artists = json.loads(ARTISTS_FILE.read_text()) if ARTISTS_FILE.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen, added = set(), []

    for a in favorites:
        key = str(a.id)
        seen.add(key)
        entry = artists.get(key)
        if entry is None:
            entry = {"id": a.id, "added_at": now}
            added.append(a.name)
        entry.update({"name": a.name, "favorite": True, "synced_at": now})
        if args.bios and not entry.get("bio"):
            try:
                entry["bio"] = clean_bio(a.get_bio())
            except Exception:
                entry["bio"] = None
        artists[key] = entry

    removed = [e["name"] for k, e in artists.items() if k not in seen and e.get("favorite", True)]
    for k, e in artists.items():
        if k not in seen:
            e["favorite"] = False

    ARTISTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARTISTS_FILE.write_text(json.dumps(artists, indent=2, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"Favorites: {len(seen)} | new: {len(added)} | unfavorited: {len(removed)}")
    if added:
        print("New:", ", ".join(added))
    if removed:
        print("Unfavorited:", ", ".join(removed))


if __name__ == "__main__":
    main()
