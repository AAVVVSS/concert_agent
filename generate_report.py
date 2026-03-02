#!/usr/bin/env python3
"""Generate an HTML concert report from upcoming_concerts.json."""

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote


def strip_tidal_markup(text: str) -> str:
    """Remove [wimpLink ...] tags from Tidal bio text."""
    text = re.sub(r"\[wimpLink[^\]]*\]", "", text)
    text = re.sub(r"\[/wimpLink\]", "", text)
    text = re.sub(r"<br/>", " ", text)
    return text.strip()


def truncate(text: str, length: int = 250) -> str:
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


def parse_date(date_str: str) -> date | None:
    """Parse YYYY-MM-DD or YYYY-MM date strings; return None for TBD/unparseable."""
    if date_str == "TBD":
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def make_ics(concert: dict, bio: str) -> str:
    artist = concert["artist_name"]
    venue = concert["venue"]
    city = concert["city"]
    country = concert["country"]
    url = concert["url"]
    date_str = concert["date"]

    dt = parse_date(date_str)
    if dt is None:
        return ""
    dt_end = dt + timedelta(days=1)
    dtstart = dt.strftime("%Y%m%d")
    dtend = dt_end.strftime("%Y%m%d")

    uid = f"{concert['artist_id']}-{date_str}@concert-agent"
    summary = f"{artist} at {venue}"
    location = f"{venue}, {city}, {country}"
    description = truncate(bio, 200) if bio else ""

    ics = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ConcertAgent//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART;VALUE=DATE:{dtstart}",
        f"DTEND;VALUE=DATE:{dtend}",
        f"SUMMARY:{summary}",
        f"LOCATION:{location}",
        f"DESCRIPTION:{description}",
        f"URL:{url}",
        "END:VEVENT",
        "END:VCALENDAR",
    ])
    return ics


def make_calendar_link(concert: dict, bio: str) -> str:
    ics = make_ics(concert, bio)
    encoded = quote(ics, safe="")
    return f'data:text/calendar;charset=utf-8,{encoded}'


def format_date_display(date_str: str) -> str:
    if date_str == "TBD":
        return "Date TBD"
    dt = parse_date(date_str)
    if dt is None:
        return date_str
    if len(date_str) == 7:  # YYYY-MM
        return dt.strftime("%b %Y")
    return dt.strftime("%a, %b %-d, %Y")


def sort_key(concert: dict):
    d = concert["date"]
    if d == "TBD":
        return date(9999, 12, 31)
    dt = parse_date(d)
    return dt if dt is not None else date(9999, 12, 30)


def generate_html(concerts: list, artist_bios: dict, output_path: Path):
    sorted_concerts = sorted(concerts, key=sort_key)
    generated = datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    total = len(sorted_concerts)

    cards_html = []
    prev_tbd = False

    for c in sorted_concerts:
        is_tbd = c["date"] == "TBD" or parse_date(c["date"]) is None

        if is_tbd and not prev_tbd:
            cards_html.append('<div class="separator"><span>Date Unknown</span></div>')
            prev_tbd = True

        artist_id = str(c["artist_id"])
        raw_bio = artist_bios.get(artist_id, {}).get("bio", "")
        bio = truncate(strip_tidal_markup(raw_bio)) if raw_bio else ""

        date_display = format_date_display(c["date"])
        venue_line = f"{c['venue']}, {c['city']}"

        cal_button = ""
        if not is_tbd:
            cal_href = make_calendar_link(c, bio)
            safe_name = re.sub(r"[^\w\-]", "_", c["artist_name"])
            cal_button = f'<a class="btn btn-cal" href="{cal_href}" download="{safe_name}.ics">📅 Add to Calendar</a>'

        source_button = f'<a class="btn btn-src" href="{c["url"]}" target="_blank" rel="noopener">🔗 Source</a>'

        bio_html = f'<p class="bio">{bio}</p>' if bio else '<p class="bio no-bio">No bio available</p>'

        card = f"""
        <div class="card{'  tbd' if is_tbd else ''}">
          <div class="card-header">
            <span class="artist">{c['artist_name']}</span>
            <span class="date-badge">{date_display}</span>
          </div>
          <div class="venue">{venue_line}</div>
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
  <title>Upcoming Concerts</title>
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
    .card-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 4px;
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
    .venue {{
      color: #aaa;
      font-size: 0.9em;
      margin-bottom: 10px;
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
    <h1>🎵 Upcoming Concerts</h1>
    <div class="meta">Generated {generated} &nbsp;·&nbsp; {total} concerts</div>
  </header>
  <div class="container">
    {''.join(cards_html)}
  </div>
  <footer>Generated by concert_agent</footer>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"Report written to {output_path}  ({total} concerts)")


def main():
    parser = argparse.ArgumentParser(description="Generate an HTML concert report.")
    parser.add_argument(
        "--concerts",
        default="upcoming_concerts.json",
        help="Path to upcoming_concerts.json (default: upcoming_concerts.json)",
    )
    parser.add_argument(
        "--artists",
        default="favorite_artists.json",
        help="Path to favorite_artists.json (default: favorite_artists.json)",
    )
    parser.add_argument(
        "--output",
        default="concert_report.html",
        help="Output HTML file path (default: concert_report.html)",
    )
    args = parser.parse_args()

    concerts_path = Path(args.concerts)
    artists_path = Path(args.artists)
    output_path = Path(args.output)

    if not concerts_path.exists():
        print(f"Error: concerts file not found: {concerts_path}")
        raise SystemExit(1)

    concerts = json.loads(concerts_path.read_text(encoding="utf-8"))

    artist_bios = {}
    if artists_path.exists():
        artist_bios = json.loads(artists_path.read_text(encoding="utf-8"))
    else:
        print(f"Warning: artists file not found ({artists_path}), bios will be omitted")

    generate_html(concerts, artist_bios, output_path)


if __name__ == "__main__":
    main()
