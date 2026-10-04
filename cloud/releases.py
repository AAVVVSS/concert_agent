"""
Weekly check for new releases (albums, EPs, singles) by the Tidal favorites.

Reads the favorites straight from Tidal, lists each artist's albums and
EPs/singles, and compares them with cloud/data/releases.json (every release
seen so far). A release counts as new when it hasn't been seen before and its
release date falls within the last NEW_WINDOW_DAYS days, which keeps catalog
uploads and reissues of old records out. Tidal sometimes lists a release
before its date; those are reported once as "coming" and again when they're out.

Each run writes cloud/runs/<date>-releases.json, rebuilds the Tidal playlist
PLAYLIST_TITLE with that run's new tracks, renders cloud/report/releases.html
and prints the phone message.

The first run (no releases.json yet) only records a baseline and reports the
releases from the last FIRST_RUN_DAYS days.

Usage:
    uv run python cloud/releases.py                 # full run
    uv run python cloud/releases.py --no-playlist   # skip the Tidal playlist
    uv run python cloud/releases.py --report-only   # re-render the page from state
"""

import argparse
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import tidal_sync  # noqa: E402

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "data" / "releases.json"
RUNS_DIR = ROOT / "runs"
REPORT_FILE = ROOT / "report" / "releases.html"
CONFIG_FILE = ROOT / "config.json"

NEW_WINDOW_DAYS = 30
FIRST_RUN_DAYS = 7
REPORT_WEEKS = 12
PLAYLIST_TITLE = "New from my favorites"
WORKERS = 6


def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def with_retry(fn, *args):
    """Call a Tidal endpoint, backing off on rate limits and network hiccups."""
    for attempt in range(5):
        try:
            return fn(*args)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return []
            time.sleep(2 ** attempt * 2)
        except (requests.RequestException, Exception) as e:  # tidalapi wraps some errors
            if "429" not in str(e) and attempt >= 2:
                raise
            time.sleep(2 ** attempt * 2)
    return fn(*args)


def kind_of(album) -> str:
    t = (album.type or "ALBUM").upper()
    return {"ALBUM": "Album", "EP": "EP", "SINGLE": "Single"}.get(t, t.title())


def as_day(d) -> str | None:
    return d.date().isoformat() if d else None


def cover_url(album) -> str | None:
    try:
        return album.image(320)
    except Exception:
        return None


def artist_releases(artist) -> list[dict]:
    """All albums and EPs/singles where this favorite is a credited artist."""
    out = []
    for getter in (artist.get_albums, artist.get_ep_singles):
        for a in with_retry(getter) or []:
            artists = [x.name for x in (a.artists or [])] or [artist.name]
            out.append({
                "id": str(a.id),
                "title": a.name,
                "kind": kind_of(a),
                "artist_id": str(artist.id),
                "artist": artist.name,
                "artists": artists,
                "release_date": as_day(a.release_date) or as_day(a.tidal_release_date),
                "tracks": a.num_tracks,
                "explicit": bool(a.explicit),
                "url": f"https://tidal.com/album/{a.id}",
                "cover": cover_url(a),
            })
    return out


def version_key(r: dict) -> tuple:
    """Tidal often lists the same record twice (explicit/clean, Dolby Atmos)."""
    title = re.sub(r"\s*[\(\[](dolby atmos|explicit|clean|clean version)[\)\]]\s*$", "", r["title"], flags=re.I)
    return (r["artist_id"], title.strip().lower(), r["release_date"])


def pick_versions(found: list[dict]) -> list[dict]:
    best: dict[tuple, dict] = {}
    for r in found:
        k = version_key(r)
        cur = best.get(k)
        if cur is None or (r["explicit"], r["tracks"] or 0) > (cur["explicit"], cur["tracks"] or 0):
            best[k] = r
    return list(best.values())


def fetch_all(session) -> tuple[list[dict], int, list[str]]:
    favorites = with_retry(session.user.favorites.artists_paginated)
    failed: list[str] = []

    def one(artist):
        try:
            return artist_releases(artist)
        except Exception as e:
            failed.append(f"{artist.name}: {type(e).__name__}")
            return []

    with ThreadPoolExecutor(WORKERS) as pool:
        batches = list(pool.map(one, favorites))
    # A collaboration shows up under each favorite on it; keep one copy.
    found: dict[str, dict] = {}
    for batch in batches:
        for r in batch:
            found.setdefault(r["id"], r)
    return list(found.values()), len(favorites), failed


def update_playlist(session, tracks_by_release: list[tuple[dict, list]]) -> str | None:
    """Rebuild PLAYLIST_TITLE with this run's new tracks, newest release first."""
    playlist = next((p for p in session.user.playlists() if p.name == PLAYLIST_TITLE), None)
    if playlist is None:
        playlist = session.user.create_playlist(
            PLAYLIST_TITLE, "New albums, EPs and singles by my favorite artists, refreshed every week.")
    else:
        playlist.clear()
    ids = [str(t.id) for _, tracks in tracks_by_release for t in tracks]
    if ids:
        playlist.add(ids, allow_duplicates=False)
    return f"https://tidal.com/playlist/{playlist.id}"


def fmt_day(d: str | None) -> str:
    if not d:
        return "date unknown"
    x = date.fromisoformat(d)
    return f"{x.strftime('%a')} {x.day} {x.strftime('%b')}"


def phone_message(run: dict) -> str:
    out, coming = run["new"], run["coming"]
    if not out and not coming:
        return "No new releases from your favorites this week."
    parts = []
    if out:
        parts.append(f"{len(out)} new release{'s' if len(out) != 1 else ''} from your favorites:")
        parts += [f"- {r['artist']}: {r['title']} ({r['kind']}, {fmt_day(r['release_date'])})" for r in out]
    if coming:
        parts.append("Coming soon:" if not out else "\nComing soon:")
        parts += [f"- {r['artist']}: {r['title']} ({r['kind']}, {fmt_day(r['release_date'])})" for r in coming]
    return "\n".join(parts)


def run(args) -> None:
    today = date.today()
    state = load(STATE_FILE, None)
    first_run = state is None
    state = state or {"releases": {}}
    known = state["releases"]

    session = tidal_sync.get_session()
    found, n_favorites, failed = fetch_all(session)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    window_start = today - timedelta(days=FIRST_RUN_DAYS if first_run else NEW_WINDOW_DAYS)

    new, coming = [], []
    for r in pick_versions([r for r in found if r["id"] not in known or known[r["id"]].get("status") == "coming"]):
        d = date.fromisoformat(r["release_date"]) if r["release_date"] else None
        prev = known.get(r["id"])
        if d and d > today:
            if prev is None:
                coming.append(r)
            status = "coming"
        elif d and d >= window_start:
            new.append(r)
            status = "new"
        else:
            status = "old"
        known[r["id"]] = {**r, "status": status, "first_seen": (prev or {}).get("first_seen", now), "reported": now if status != "old" else (prev or {}).get("reported")}
    # Everything else Tidal listed is now known, so it never counts as new later.
    for r in found:
        if r["id"] not in known:
            known[r["id"]] = {**r, "status": "old", "first_seen": now, "reported": None}
    new.sort(key=lambda r: (r["release_date"], r["artist"].lower()), reverse=True)
    coming.sort(key=lambda r: r["release_date"])

    playlist_url = state.get("playlist_url")
    if not args.no_playlist:
        try:
            with_tracks = []
            for r in new:
                album = session.album(int(r["id"]))
                with_tracks.append((r, with_retry(album.tracks)))
            playlist_url = update_playlist(session, with_tracks)
        except Exception as e:
            failed.append(f"playlist: {type(e).__name__}: {e}")

    run_info = {
        "date": today.isoformat(),
        "ran_at": now,
        "first_run": first_run,
        "favorites": n_favorites,
        "releases_listed": len(found),
        "new": new,
        "coming": coming,
        "failed": failed,
        "playlist_url": playlist_url,
    }
    state.update({"last_run": run_info["ran_at"], "playlist_url": playlist_url})
    save(STATE_FILE, state)
    save(RUNS_DIR / f"{today.isoformat()}-releases.json", run_info)
    render(state, run_info)

    print(f"Favorites: {n_favorites} | releases listed: {len(found)} | new: {len(new)} | coming: {len(coming)} | failed: {len(failed)}")
    for f in failed:
        print("  failed:", f)
    print("\n" + phone_message(run_info))


def report_only(args) -> None:
    state = load(STATE_FILE, None)
    if state is None:
        sys.exit("No releases.json yet; do a full run first.")
    runs = sorted(RUNS_DIR.glob("*-releases.json"))
    render(state, load(runs[-1], {}) if runs else {})


# ---------- report page ----------

def render(state: dict, last_run: dict) -> None:
    today = date.today()
    since = today - timedelta(weeks=REPORT_WEEKS)
    fresh = {r["id"] for r in last_run.get("new", []) + last_run.get("coming", [])}
    shown = [r for r in state["releases"].values() if r.get("reported") and r.get("release_date")
             and date.fromisoformat(r["release_date"]) >= since]
    coming = sorted([r for r in shown if date.fromisoformat(r["release_date"]) > today], key=lambda r: r["release_date"])
    out = sorted([r for r in shown if date.fromisoformat(r["release_date"]) <= today],
                 key=lambda r: (r["release_date"], r["artist"].lower()), reverse=True)

    def week_of(d: str) -> date:
        x = date.fromisoformat(d)
        return x - timedelta(days=x.weekday())

    def item(r: dict) -> str:
        e = html.escape
        names = ", ".join(r["artists"]) if r.get("artists") else r["artist"]
        img = (f'<img src="{e(r["cover"])}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{{className:\'nocover\'}}))">'
               if r.get("cover") else '<span class="nocover"></span>')
        chips = f'<span class="chip k-{r["kind"].lower()}">{e(r["kind"])}</span>'
        if r["id"] in fresh:
            chips += '<span class="chip s-new">New</span>'
        tracks = f' · {r["tracks"]} track{"s" if r["tracks"] != 1 else ""}' if r.get("tracks") else ""
        return (f'<li class="rel">{img}<div class="what"><h3><a href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["title"])}</a></h3>'
                f'<p class="who">{e(names)}</p><p class="when">{fmt_day(r["release_date"])}{tracks}</p></div>'
                f'<div class="chips">{chips}</div></li>')

    sections = []
    if coming:
        sections.append(f'<section><h2>Coming soon<span class="count">{len(coming)}</span></h2><ol>{"".join(map(item, coming))}</ol></section>')
    weeks: dict[date, list] = {}
    for r in out:
        weeks.setdefault(week_of(r["release_date"]), []).append(r)
    for wk, rs in weeks.items():
        label = f"Week of {wk.day} {wk.strftime('%b %Y')}"
        sections.append(f'<section><h2>{label}<span class="count">{len(rs)}</span></h2><ol>{"".join(map(item, rs))}</ol></section>')
    if not sections:
        sections.append('<p class="empty">No releases from your favorites in the last few weeks.</p>')

    lr = last_run
    meta = ""
    if lr:
        when = date.fromisoformat(lr["date"])
        meta = (f'Last check {when.day} {when.strftime("%b %Y")}: {len(lr.get("new", []))} new'
                f'{", " + str(len(lr["coming"])) + " coming" if lr.get("coming") else ""} · {lr.get("favorites", "?")} artists')
    playlist = state.get("playlist_url")
    playlist_html = (f'<a class="play" href="{html.escape(playlist)}" target="_blank" rel="noopener">Play this week\'s new tracks on Tidal</a>'
                     if playlist else "")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(PAGE.replace("{{META}}", html.escape(meta)).replace("{{PLAYLIST}}", playlist_html)
                           .replace("{{SECTIONS}}", "\n".join(sections)))


PAGE = """<title>Fresh Drops</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@500&display=swap">
<style>
/* Same visual family as Swiss Gig Radar: one narrow column, week headings, a row per release with its cover on the left. */
:root {
  --bg: #f4f3f6; --surface: #ffffff; --fg: #1b1a21; --muted: #605d6b; --line: #dcdae2;
  --accent: #5b3fb5; --new: #b4421b; --off: #8f8c99;
  --display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #131218; --surface: #1c1b23; --fg: #e8e6ee; --muted: #a29fae; --line: #2d2b36;
  --accent: #a891f0; --new: #f08a5d; --off: #6f6c79; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #131218; --surface: #1c1b23; --fg: #e8e6ee; --muted: #a29fae; --line: #2d2b36;
  --accent: #a891f0; --new: #f08a5d; --off: #6f6c79; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font: 15px/1.5 var(--body); margin: 0; }
.wrap { max-width: 46rem; margin: 0 auto; padding-inline: 16px; padding-block: 2rem 4rem; }
header h1 { font: 700 clamp(1.9rem, 6vw, 2.6rem)/1.05 var(--display); letter-spacing: -0.02em; margin: 0; }
header p { color: var(--muted); margin: .5rem 0 0; }
.meta { font: 500 .75rem var(--mono); text-transform: uppercase; letter-spacing: .06em; color: var(--accent); margin-top: 1rem; }
.play { display: inline-block; margin-top: 1rem; padding: .5rem .9rem; border-radius: 99px; background: var(--accent); color: var(--bg); font-weight: 500; text-decoration: none; }
.play:hover, .play:focus-visible { filter: brightness(1.1); outline: none; }
section { margin-top: 2.25rem; }
h2 { font: 500 1.15rem var(--display); display: flex; align-items: baseline; gap: .6rem; border-bottom: 1px solid var(--line); padding-bottom: .4rem; margin: 0; }
.count { font: 500 .75rem var(--mono); color: var(--muted); }
ol { list-style: none; margin: 0; padding: 0; }
.rel { display: grid; grid-template-columns: 3.5rem minmax(0, 1fr) auto; gap: .9rem; align-items: center; padding: .75rem 0; border-bottom: 1px solid var(--line); }
.rel img, .nocover { width: 3.5rem; height: 3.5rem; border-radius: 4px; object-fit: cover; background: var(--surface); border: 1px solid var(--line); display: block; }
h3 { font: 500 1.02rem/1.3 var(--body); margin: 0; overflow-wrap: anywhere; }
h3 a { color: inherit; text-decoration-color: var(--line); text-underline-offset: 3px; }
h3 a:hover, h3 a:focus-visible { text-decoration-color: var(--accent); outline: none; }
.who { margin: .1rem 0 0; }
.when { color: var(--muted); font-size: .85rem; margin: .1rem 0 0; }
.chips { display: flex; flex-wrap: wrap; gap: .3rem; justify-content: flex-end; max-width: 9rem; }
.chip { font: 500 .66rem var(--mono); text-transform: uppercase; letter-spacing: .05em; padding: .15rem .45rem; border-radius: 99px; border: 1px solid currentColor; white-space: nowrap; color: var(--muted); }
.k-album { color: var(--accent); }
.s-new { color: var(--bg); background: var(--new); border-color: var(--new); }
.empty { color: var(--muted); margin-top: 2rem; }
@media (max-width: 30rem) {
  .rel { grid-template-columns: 3rem minmax(0, 1fr); }
  .rel img, .nocover { width: 3rem; height: 3rem; }
  .chips { grid-column: 2; justify-content: flex-start; max-width: none; }
}
</style>
<div class="wrap">
<header>
  <h1>Fresh Drops</h1>
  <p>New albums, EPs and singles by your Tidal favorites, last 12 weeks.</p>
  <div class="meta">{{META}}</div>
  {{PLAYLIST}}
</header>
{{SECTIONS}}
</div>
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--no-playlist", action="store_true", help="don't touch the Tidal playlist")
    p.add_argument("--report-only", action="store_true", help="re-render the page from releases.json")
    args = p.parse_args()
    (report_only if args.report_only else run)(args)


if __name__ == "__main__":
    main()
