"""
State and report tooling for the scheduled concert runs (see cloud/README.md).

The research itself is done by Claude following cloud/prompts/*.md; this script
keeps the bookkeeping deterministic:

    python cloud/concerts.py due [--days 30]          artists due for deep research
    python cloud/concerts.py match EVENTS.json        venue-calendar events -> favorite artists
    python cloud/concerts.py mark RESEARCHED.json     record deep-research results per artist
    python cloud/concerts.py merge FINDINGS.json --kind weekly|monthly
                                                      upsert concerts, write run summary,
                                                      print the phone message
    python cloud/concerts.py report                   render cloud/report/index.html
    python cloud/concerts.py profiles-due             favorites without a bio/tags profile
    python cloud/concerts.py profiles-merge NEW.json  add or replace artist profiles

Only the standard library is used so it runs anywhere.
"""

import argparse
import html
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
ARTISTS_FILE = ROOT / "data" / "favorite_artists.json"
CONCERTS_FILE = ROOT / "data" / "concerts.json"
PROFILES_FILE = ROOT / "data" / "artist_profiles.json"
RUNS_DIR = ROOT / "runs"
REPORT_FILE = ROOT / "report" / "index.html"

HORIZON_DAYS = 183  # six months
STATUSES = {"confirmed", "tentative", "festival_pending", "cancelled"}
GENRES = {
    "rock", "indie", "pop", "electronic", "hip hop", "r&b", "soul", "jazz", "folk", "country",
    "blues", "metal", "punk", "latin", "reggae", "world", "ambient", "experimental", "classical",
    "soundtrack",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Sigur Rós' -> 'sigur ros'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("$", "s")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def norm_venue(venue: str) -> str:
    v = norm(venue)
    for suffix in (" house of music", " klubsaal", " klub", " club", " saal", " stage", " arena"):
        if v.endswith(suffix) and len(v) > len(suffix):
            v = v[: -len(suffix)]
    return v


def concert_id(artist_id, venue: str, day: str) -> str:
    return f"{artist_id}|{norm_venue(venue)}|{day}"


def favorites() -> dict:
    return {k: a for k, a in load(ARTISTS_FILE, {}).items() if a.get("favorite", True)}


# ---------------------------------------------------------------------------
# due: which artists the monthly deep pass should research
# ---------------------------------------------------------------------------

def cmd_due(args) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    due = []
    for a in favorites().values():
        if a.get("permanently_inactive"):
            continue
        last = a.get("researched_at")
        if not last or datetime.fromisoformat(last) < cutoff:
            due.append({"id": a["id"], "name": a["name"], "researched_at": last})
    due.sort(key=lambda a: (a["researched_at"] is not None, a["researched_at"] or "", a["name"].lower()))
    json.dump(due, sys.stdout, indent=2, ensure_ascii=False)
    print(f"\n{len(due)} artists due", file=sys.stderr)


# ---------------------------------------------------------------------------
# match: venue-calendar events -> favorite artists (weekly sweep)
# ---------------------------------------------------------------------------

def cmd_match(args) -> None:
    """EVENTS.json: [{"title", "date", "venue", "city", "url"}, ...] as read from calendars.

    Matches on whole words of the normalized artist name, so "toe" does not hit
    "Toes" or "Mistletoe". Very short names (<= 3 chars) must open the
    title (the headliner slot) to avoid noise. Claude reviews every match.
    """
    events = load(Path(args.events), [])
    artists = [(a, norm(a["name"])) for a in favorites().values() if not a.get("permanently_inactive")]
    matches = []
    for ev in events:
        title = norm(ev.get("title", ""))
        if not title:
            continue
        for a, name in artists:
            if not name:
                continue
            if len(name) <= 3:
                hit = title == name or title.startswith(name + " ")
            else:
                hit = re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", title) is not None
            if hit:
                matches.append({"artist_id": a["id"], "artist_name": a["name"], **ev})
    json.dump(matches, sys.stdout, indent=2, ensure_ascii=False)
    print(f"\n{len(matches)} matches from {len(events)} events", file=sys.stderr)


# ---------------------------------------------------------------------------
# mark: record deep-research results on artists
# ---------------------------------------------------------------------------

def cmd_mark(args) -> None:
    """RESEARCHED.json: [{"id", "active": bool, "active_reason", "permanently_inactive": bool}]"""
    artists = load(ARTISTS_FILE, {})
    stamp = now_iso()
    n = 0
    for r in load(Path(args.researched), []):
        a = artists.get(str(r["id"]))
        if a is None:
            continue
        for field in ("active", "active_reason", "permanently_inactive"):
            if field in r:
                a[field] = r[field]
        a["researched_at"] = stamp
        n += 1
    save(ARTISTS_FILE, artists)
    print(f"Marked {n} artists researched")


# ---------------------------------------------------------------------------
# merge: upsert verified findings and summarize what changed
# ---------------------------------------------------------------------------

def cmd_merge(args) -> None:
    """FINDINGS.json: verified concerts only, each
    {"artist_id", "artist_name", "date": "YYYY-MM-DD", "venue", "city",
     "kind": "concert"|"openair", "url", "status", "notes"}.
    A rescheduled or moved show may carry "replaces": "<id of the old entry>".
    """
    findings = load(Path(args.findings), [])
    concerts = {c["id"]: c for c in load(CONCERTS_FILE, [])}
    stamp = now_iso()
    today = date.today().isoformat()
    new, changed = [], []

    for f in findings:
        status = f.get("status", "confirmed")
        if status not in STATUSES:
            sys.exit(f"Unknown status {status!r} for {f.get('artist_name')}")
        cid = concert_id(f["artist_id"], f["venue"], f["date"])
        old_id = f.get("replaces") if f.get("replaces") in concerts else None
        if old_id is None and cid not in concerts:
            old_id = _guess_previous(concerts, f, findings)
        record = {
            "id": cid,
            "artist_id": f["artist_id"],
            "artist_name": f["artist_name"],
            "date": f["date"],
            "venue": f["venue"],
            "city": f["city"],
            "kind": f.get("kind", "concert"),
            "url": f.get("url", ""),
            "status": status,
            "notes": f.get("notes", ""),
        }
        prev = concerts.get(cid) or (concerts.pop(old_id) if old_id else None)
        if prev is None:
            record.update(first_seen=stamp, last_seen=stamp, last_changed=stamp, history=[])
            concerts[cid] = record
            new.append(record)
            continue
        diffs = [
            {"field": k, "old": prev.get(k), "new": record[k]}
            for k in ("date", "venue", "city", "status")
            if norm(str(prev.get(k))) != norm(str(record[k]))
        ]
        record.update(
            first_seen=prev.get("first_seen", stamp),
            last_seen=stamp,
            last_changed=stamp if diffs else prev.get("last_changed", stamp),
            history=prev.get("history", []) + [{"at": stamp, **d} for d in diffs],
        )
        concerts[cid] = record
        if diffs:
            changed.append({**record, "changes": diffs})

    ordered = sorted(concerts.values(), key=lambda c: (c["date"], c["artist_name"].lower()))
    save(CONCERTS_FILE, ordered)

    horizon = (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()
    in_window = lambda c: today <= c["date"] <= horizon
    new = [c for c in new if in_window(c)]
    changed = [c for c in changed if in_window(c)]
    summary = {
        "kind": args.kind,
        "ran_at": stamp,
        "findings": len(findings),
        "new": new,
        "changed": changed,
        "message": phone_message(args.kind, new, changed),
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    save(RUNS_DIR / f"{date.today().isoformat()}-{args.kind}.json", summary)
    print(summary["message"])


def _guess_previous(concerts: dict, f: dict, batch: list) -> str | None:
    """Find the entry a reschedule or venue move replaces, if unambiguous.

    Same artist + same date at a different venue -> venue moved.
    Same artist + same venue at a different date, and the old date is not
    also in this batch -> rescheduled.
    """
    batch_ids = {concert_id(b["artist_id"], b["venue"], b["date"]) for b in batch}
    same_artist = [c for c in concerts.values() if c["artist_id"] == f["artist_id"] and c["id"] not in batch_ids]
    by_date = [c for c in same_artist if c["date"] == f["date"]]
    if len(by_date) == 1:
        return by_date[0]["id"]
    by_venue = [c for c in same_artist if norm_venue(c["venue"]) == norm_venue(f["venue"]) and c["date"] >= date.today().isoformat()]
    if len(by_venue) == 1:
        return by_venue[0]["id"]
    return None


def _fmt_day(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-d %b")


def phone_message(kind: str, new: list, changed: list) -> str:
    if not new and not changed:
        return f"Concerts ({kind}): nothing new or changed."
    parts = []
    for c in new[:6]:
        parts.append(f"{c['artist_name']} {c['venue']} {_fmt_day(c['date'])}")
    for c in changed[:4]:
        what = ", ".join(d["field"] for d in c["changes"])
        parts.append(f"{c['artist_name']} {what} changed")
    more = len(new) + len(changed) - len(parts)
    head = f"{len(new)} new, {len(changed)} changed: "
    msg = head + "; ".join(parts) + (f" +{more} more" if more > 0 else "")
    return msg if len(msg) <= 200 else msg[:197] + "..."


# ---------------------------------------------------------------------------
# report: full upcoming list as a self-contained HTML page
# ---------------------------------------------------------------------------

STATUS_LABEL = {
    "confirmed": "Confirmed",
    "tentative": "Tentative",
    "festival_pending": "Lineup pending",
    "cancelled": "Cancelled",
}


def cmd_profiles_due(args) -> None:
    profiles = load(PROFILES_FILE, {})
    due = [{"id": a["id"], "name": a["name"]} for k, a in favorites().items() if k not in profiles]
    due.sort(key=lambda a: a["name"].lower())
    json.dump(due, sys.stdout, indent=2, ensure_ascii=False)
    print(f"\n{len(due)} artists without a profile", file=sys.stderr)


def cmd_profiles_merge(args) -> None:
    profiles = load(PROFILES_FILE, {})
    new = load(Path(args.profiles), {})
    errors = []
    for k, p in new.items():
        bad = [g for g in p.get("genres", []) if g not in GENRES]
        if bad or not p.get("bio") or not p.get("name"):
            errors.append(f"{k} {p.get('name')}: missing bio/name or unknown genres {bad}")
    if errors:
        sys.exit("Not merged:\n" + "\n".join(errors))
    profiles.update({str(k): p for k, p in new.items()})
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"Merged {len(new)} profiles ({len(profiles)} total)")


def cmd_report(args) -> None:
    concerts = load(CONCERTS_FILE, [])
    runs = sorted(RUNS_DIR.glob("*.json")) if RUNS_DIR.exists() else []
    last_run = load(runs[-1], {}) if runs else {}
    fresh = {c["id"] for c in last_run.get("new", [])} | {c["id"] for c in last_run.get("changed", [])}
    today = date.today()
    horizon = today + timedelta(days=HORIZON_DAYS)
    upcoming = [c for c in concerts if today.isoformat() <= c["date"] <= horizon.isoformat()]
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(render(upcoming, fresh, last_run, today, horizon, load(PROFILES_FILE, {})))
    print(f"Wrote {REPORT_FILE} ({len(upcoming)} concerts)")


def render(upcoming: list, fresh: set, last_run: dict, today: date, horizon: date, profiles: dict) -> str:
    e = html.escape
    months: dict[str, list] = {}
    for c in upcoming:
        months.setdefault(c["date"][:7], []).append(c)

    sections = []
    for ym, items in months.items():
        label = datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
        rows = []
        for c in items:
            d = datetime.strptime(c["date"], "%Y-%m-%d")
            chips = [f'<span class="chip s-{e(c["status"])}">{e(STATUS_LABEL.get(c["status"], c["status"]))}</span>']
            if c["kind"] == "openair":
                chips.append('<span class="chip s-openair">Open air</span>')
            if c["id"] in fresh:
                chips.insert(0, '<span class="chip s-new">New</span>' if c["first_seen"] == c["last_changed"] else '<span class="chip s-new">Changed</span>')
            artist = e(c["artist_name"])
            if c.get("url"):
                artist = f'<a href="{e(c["url"])}" target="_blank" rel="noopener">{artist}</a>'
            note = f'<p class="note">{e(c["notes"])}</p>' if c.get("notes") else ""
            prof = profiles.get(str(c["artist_id"]), {})
            if prof.get("styles"):
                note = f'<p class="tags">{e(" · ".join(prof["styles"]))}</p>' + note
            if prof.get("bio"):
                note += f'<details><summary>About</summary><p>{e(prof["bio"])}</p></details>'
            rows.append(
                f'<li class="gig{" off" if c["status"] == "cancelled" else ""}">'
                f'<time datetime="{c["date"]}"><span class="dow">{d.strftime("%a")}</span><span class="dom">{d.day}</span></time>'
                f'<div class="what"><h3>{artist}</h3><p class="where">{e(c["venue"])} · {e(c["city"])}</p>{note}</div>'
                f'<div class="chips">{"".join(chips)}</div></li>'
            )
        sections.append(f'<section><h2>{label}<span class="count">{len(items)}</span></h2><ol>{"".join(rows)}</ol></section>')

    if last_run:
        ran = datetime.fromisoformat(last_run["ran_at"]).strftime("%-d %b %Y")
        run_line = f'Last {e(last_run["kind"])} run {ran}: {len(last_run["new"])} new, {len(last_run["changed"])} changed.'
    else:
        run_line = "No run yet."
    body = "".join(sections) or '<p class="empty">No concerts found yet in the next six months. The weekly sweep fills this list.</p>'

    return f"""<title>Swiss Gig Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@500&display=swap">
<style>
/* Layout: one narrow column, month headings, each gig a row with a date stub on the left. */
:root {{
  --bg: #f3f5f4; --surface: #ffffff; --fg: #17201d; --muted: #5b6863; --line: #d9dfdc;
  --accent: #0d6b5f; --new: #b4421b; --warn: #8a6100; --off: #8b9490;
  --display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #111816; --surface: #18211e; --fg: #e4ebe8; --muted: #9aa8a2; --line: #2a3632;
  --accent: #4fc2b0; --new: #f08a5d; --warn: #e2b54c; --off: #6c7772; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #111816; --surface: #18211e; --fg: #e4ebe8; --muted: #9aa8a2; --line: #2a3632;
  --accent: #4fc2b0; --new: #f08a5d; --warn: #e2b54c; --off: #6c7772; color-scheme: dark }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.5 var(--body); }}
.wrap {{ max-width: 46rem; margin: 0 auto; padding-inline: 16px; padding-block: 2rem 4rem; }}
header h1 {{ font: 700 clamp(1.9rem, 6vw, 2.6rem)/1.05 var(--display); letter-spacing: -0.02em; margin: 0; text-wrap: balance; }}
header p {{ color: var(--muted); margin: .5rem 0 0; }}
.meta {{ font: 500 .75rem var(--mono); text-transform: uppercase; letter-spacing: .06em; color: var(--accent); margin-top: 1rem; }}
section {{ margin-top: 2.25rem; }}
h2 {{ font: 500 1.15rem var(--display); display: flex; align-items: baseline; gap: .6rem; border-bottom: 1px solid var(--line); padding-bottom: .4rem; margin: 0; }}
.count {{ font: 500 .75rem var(--mono); color: var(--muted); }}
ol {{ list-style: none; margin: 0; padding: 0; }}
.gig {{ display: grid; grid-template-columns: 3.2rem minmax(0, 1fr) auto; gap: .9rem; align-items: start; padding: .85rem 0; border-bottom: 1px solid var(--line); }}
time {{ display: flex; flex-direction: column; align-items: center; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: .3rem 0; font-variant-numeric: tabular-nums; }}
.dow {{ font: 500 .65rem var(--mono); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }}
.dom {{ font: 700 1.3rem/1.1 var(--display); }}
h3 {{ font: 500 1.02rem/1.3 var(--body); margin: 0; }}
h3 a {{ color: inherit; text-decoration-color: var(--line); text-underline-offset: 3px; }}
h3 a:hover, h3 a:focus-visible {{ text-decoration-color: var(--accent); outline: none; }}
.where {{ color: var(--muted); margin: .1rem 0 0; }}
.note {{ color: var(--muted); font-size: .85rem; margin: .25rem 0 0; }}
.tags {{ font: 500 .72rem var(--mono); color: var(--accent); margin: .2rem 0 0; }}
details {{ font-size: .85rem; margin-top: .3rem; }}
summary {{ color: var(--muted); cursor: pointer; }}
details p {{ margin: .3rem 0 0; }}
.chips {{ display: flex; flex-wrap: wrap; gap: .3rem; justify-content: flex-end; max-width: 9rem; }}
.chip {{ font: 500 .66rem var(--mono); text-transform: uppercase; letter-spacing: .05em; padding: .15rem .45rem; border-radius: 99px; border: 1px solid currentColor; white-space: nowrap; }}
.s-confirmed {{ color: var(--accent); }}
.s-tentative, .s-festival_pending {{ color: var(--warn); }}
.s-cancelled {{ color: var(--off); }}
.s-openair {{ color: var(--muted); }}
.s-new {{ color: var(--bg); background: var(--new); border-color: var(--new); }}
.off h3, .off .where {{ text-decoration: line-through; color: var(--off); }}
.empty {{ color: var(--muted); margin-top: 2rem; }}
@media (max-width: 30rem) {{
  .gig {{ grid-template-columns: 3rem minmax(0, 1fr); }}
  .chips {{ grid-column: 2; justify-content: flex-start; max-width: none; }}
}}
</style>
<div class="wrap">
<header>
  <h1>Swiss Gig Radar</h1>
  <p>Your Tidal favorites playing in German-speaking Switzerland, {today.strftime("%-d %b %Y")} to {horizon.strftime("%-d %b %Y")}.</p>
  <div class="meta">{len(upcoming)} shows · {e(run_line)}</div>
</header>
{body}
</div>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Concert state and report tooling")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("due"); s.add_argument("--days", type=int, default=30); s.set_defaults(fn=cmd_due)
    s = sub.add_parser("match"); s.add_argument("events"); s.set_defaults(fn=cmd_match)
    s = sub.add_parser("mark"); s.add_argument("researched"); s.set_defaults(fn=cmd_mark)
    s = sub.add_parser("merge"); s.add_argument("findings"); s.add_argument("--kind", choices=["weekly", "monthly"], required=True); s.set_defaults(fn=cmd_merge)
    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("profiles-due"); s.set_defaults(fn=cmd_profiles_due)
    s = sub.add_parser("profiles-merge"); s.add_argument("profiles"); s.set_defaults(fn=cmd_profiles_merge)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
