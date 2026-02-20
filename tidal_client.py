import json
import tidalapi

SESSION_FILE = "tidal_session.json"


def get_session() -> tidalapi.Session:
    """
    Return an authenticated Tidal session.
    On first run, initiates OAuth device-code login (prints a URL to visit).
    On subsequent runs, loads saved credentials from SESSION_FILE.
    """
    session = tidalapi.Session()

    # Try to load a previously saved session
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        session.load_oauth_session(
            data["token_type"],
            data["access_token"],
            data["refresh_token"],
            data["expiry_time"],
        )
        if session.check_login():
            return session
    except (FileNotFoundError, KeyError):
        pass

    # First-time login: device-code OAuth flow (no app registration needed)
    session.login_oauth_simple()

    # Persist credentials for future runs
    with open(SESSION_FILE, "w") as f:
        json.dump(
            {
                "token_type": session.token_type,
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expiry_time": str(session.expiry_time),
            },
            f,
            indent=2,
        )

    return session


def get_favorite_artists(session: tidalapi.Session) -> list[dict]:
    """
    Return a list of the user's favorite artists as plain dicts.
    Each dict contains: id, name.
    """
    artists = session.user.favorites.artists_paginated()
    result = []
    for a in artists:
        try:
            bio = a.get_bio()
        except Exception:
            bio = None
        result.append({"id": a.id, "name": a.name, "bio": bio})
    return result
