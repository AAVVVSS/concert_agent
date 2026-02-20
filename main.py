import json
from datetime import datetime, timezone
from tidal_client import get_session, get_favorite_artists

ARTISTS_FILE = "favorite_artists.json"


def load_local_artists() -> dict:
    try:
        with open(ARTISTS_FILE) as f:
            data = json.load(f)
        # Migrate from old list format if needed
        if isinstance(data, list):
            return {str(a["id"]): a for a in data}
        return data
    except FileNotFoundError:
        return {}


def main():
    session = get_session()
    print(f"Logged in as: {session.user.first_name} {session.user.last_name}")

    local = load_local_artists()
    fresh = get_favorite_artists(session)
    print(f"Found {len(fresh)} favorite artists on Tidal.")

    now = datetime.now(timezone.utc).isoformat()
    for artist in fresh:
        key = str(artist["id"])
        entry = local.get(key, {})
        # Refresh Tidal-sourced fields; any other keys in entry are preserved
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


if __name__ == "__main__":
    main()
