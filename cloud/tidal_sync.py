"""
Sync Tidal favorite artists into cloud/data/favorite_artists.json.

Cloud-friendly variant of ../tidal_client.py. Authenticates with a refresh
token, taken from the TIDAL_REFRESH_TOKEN environment variable if set, else
from the session file in the project's private shared folder (TOKEN_FILE, same
format as ../tidal_session.json). Each successful refresh is written back.

`--login` runs Tidal's device-code login once: it prints a link.tidal.com URL
for the account owner to approve, waits, and saves the refresh token to
TOKEN_FILE. The file lives outside the repo and must never be committed.

Tidal-sourced fields (name) are refreshed; every other field on an artist
(research status, notes, ...) is preserved. Artists that were unfavorited are
kept but flagged with "favorite": false so their history survives.

Usage:
    uv run python cloud/tidal_sync.py            # sync, no bios
    uv run python cloud/tidal_sync.py --bios     # also fetch bios for artists missing one
    uv run python -u cloud/tidal_sync.py --login # one-time login, saves the session file
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import tidalapi
import tidalapi.exceptions

ARTISTS_FILE = Path(__file__).parent / "data" / "favorite_artists.json"
TOKEN_FILE = Path(os.environ.get("TIDAL_TOKEN_FILE", "/mnt/project-files/tidal/tidal_session.json"))


def save_session(session: tidalapi.Session, refresh_token: str) -> None:
    """Write the session in tidal_client.py's tidal_session.json format (never logged)."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": refresh_token,
        "expiry_time": str(session.expiry_time),
    }, indent=2) + "\n")
    TOKEN_FILE.chmod(0o600)


def login() -> None:
    session = tidalapi.Session()
    session.login_oauth_simple(fn_print=lambda text: print(text, flush=True))
    save_session(session, session.refresh_token)
    print(f"Logged in; session saved to {TOKEN_FILE}")


def read_refresh_token() -> str:
    token = os.environ.get("TIDAL_REFRESH_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        token = json.loads(TOKEN_FILE.read_text()).get("refresh_token", "").strip()
    return token


def get_session() -> tidalapi.Session:
    refresh_token = read_refresh_token()
    if not refresh_token:
        sys.exit(f"No Tidal token: set TIDAL_REFRESH_TOKEN or run --login to create {TOKEN_FILE}.")
    session = tidalapi.Session()
    try:
        ok = session.token_refresh(refresh_token)
    except tidalapi.exceptions.AuthenticationError as e:
        sys.exit(f"Tidal token refresh failed ({e}). A new Tidal login is needed.")
    except requests.RequestException as e:
        sys.exit(f"Could not reach Tidal ({type(e).__name__}); check the environment's network allowlist.")
    if not ok or not session.load_oauth_session(
        session.token_type, session.access_token, refresh_token, session.expiry_time
    ):
        sys.exit("Tidal login failed after token refresh. A new Tidal login is needed.")
    if TOKEN_FILE.parent.exists():
        save_session(session, refresh_token)
    return session


def clean_bio(bio: str | None) -> str | None:
    if not bio:
        return None
    return re.sub(r"\[/?wimpLink[^\]]*\]", "", bio).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--bios", action="store_true", help="fetch bios for artists without one")
    parser.add_argument("--login", action="store_true", help="one-time device-code login; saves the session file")
    args = parser.parse_args()

    if args.login:
        login()
        return

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
