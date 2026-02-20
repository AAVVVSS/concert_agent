# Concert Agent

Tracks your Tidal favorite artists and finds upcoming concerts near Zürich, Switzerland.

## What it does

- Syncs your full list of favorite artists from Tidal (including bios)
- Researches whether each artist is still active (alive / band not disbanded)
- For active artists, searches for upcoming shows in the greater Zürich area
- Stores everything locally so repeated runs only update what has changed

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure API keys

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
TAVILY_API_KEY=tvly-...
```

- **Anthropic key**: [console.anthropic.com](https://console.anthropic.com)
- **Tavily key**: [app.tavily.com](https://app.tavily.com) — free tier gives 1,000 searches/month

### 3. Tidal login

On first run of `main.py`, you will be prompted to authenticate via a device-code URL. Visit the URL, log in, and the session is saved to `tidal_session.json` for future runs.

---

## Workflow

### Step 1 — Sync favorites from Tidal

```bash
python main.py
```

Fetches your full list of favorite artists (all pages) and their bios from Tidal. Results are saved to `favorite_artists.json`. Re-running updates the Tidal-sourced fields (`name`, `bio`) while preserving any custom metadata you have added manually.

### Step 2 — Research activity & find concerts

```bash
python research_artists.py
```

For each artist not yet checked (or not checked in the last 30 days), a Claude agent:
1. Reads the stored bio
2. Looks up the artist on MusicBrainz if the bio is ambiguous
3. Does a targeted web search if needed
4. Determines active status and searches for Zürich-area concerts

Results are saved back to `favorite_artists.json` (status fields) and to `upcoming_concerts.json` (concert events).

**On subsequent runs**, already-researched artists are skipped automatically:
- Artists checked within the last 30 days → skipped
- Permanently inactive artists (deceased / definitively disbanded) → skipped forever

---

## Data files

### `favorite_artists.json`

Dict keyed by Tidal artist ID. Each entry:

```json
"14630": {
  "id": 14630,
  "name": "10,000 Maniacs",
  "bio": "...",
  "last_updated": "2026-02-20T20:35:36+00:00",

  "active": true,
  "active_reason": "Still touring; celebrated 40th anniversary in 2022.",
  "active_checked_at": "2026-02-20T21:00:00+00:00",
  "permanently_inactive": false
}
```

**You can add any extra fields directly** — they will be preserved across Tidal syncs and research runs. For example:

```json
"seen_live": true,
"notes": "Amazing live show at Rote Fabrik 2019"
```

**`permanently_inactive`**: set to `true` automatically for artists where no live show is ever possible (deceased, definitively disbanded). These are never re-researched.

### `upcoming_concerts.json`

Flat list of found concerts near Zürich:

```json
[
  {
    "artist_id": 4315444,
    "artist_name": "A$AP Rocky",
    "date": "2026-05-09",
    "venue": "Hallenstadion",
    "city": "Zürich",
    "country": "Switzerland",
    "url": "https://...",
    "searched_at": "2026-02-20T21:34:51+00:00"
  }
]
```

Concert entries for an artist are replaced each time that artist is re-researched.

---

## Useful commands

### Research specific artists (bypasses cache)

```bash
python research_artists.py --names "Portishead,Massive Attack,Blur"
```

Useful for spot-checking or re-researching artists regardless of when they were last checked.

### Test run on a limited number of artists

```bash
python research_artists.py --limit 10
```

Processes the first 10 artists not yet checked. Good for testing before a full run.

### Full run

```bash
python research_artists.py
```

Processes all unchecked / stale artists. With 640 artists, expect a first full run to take a while (one Claude + web search call per active artist).

---

## Recommended update schedule

| Script | How often | Why |
|--------|-----------|-----|
| `main.py` | Monthly | Picks up newly favorited artists and refreshed bios |
| `research_artists.py` | Monthly | The 30-day cache expires; finds new tour announcements |

Run them in sequence:

```bash
python main.py && python research_artists.py
```
