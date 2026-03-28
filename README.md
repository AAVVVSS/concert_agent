# Concert Agent

Tracks your Tidal favorite artists and finds upcoming concerts near Zürich, Switzerland.

## What it does

1. **Syncs** your full list of favorite artists from Tidal (including bios)
2. **Researches** whether each artist is still active (alive / band not disbanded)
3. **Finds** upcoming shows in the greater Zürich area for active artists
4. **Verifies** found concerts against their source URLs and web searches
5. **Generates** HTML reports (both raw and verified)

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

- **Anthropic key**: [console.anthropic.com](https://console.anthropic.com) — needed for research (unless using `--local`) and verification
- **Tavily key**: [app.tavily.com](https://app.tavily.com) — free tier gives 1,000 searches/month (always required)

### 2b. (Optional) Set up Ollama for local inference

Install [Ollama](https://ollama.com) and pull the models:

```bash
ollama pull qwen3:32b   # main agent (reasoning + tool use)
ollama pull qwen3:8b    # page parser (lightweight HTML extraction)
```

Ollama must be running (`ollama serve`) before using local mode or the page parser.

### 3. Tidal login

On first run, you will be prompted to authenticate via a device-code URL. Visit the URL, log in, and the session is saved to `tidal_session.json` for future runs.

---

## Usage

### Run the full pipeline

```bash
python main.py
```

This runs all steps in sequence: Tidal sync → artist research → concert verification → report generation. Configure which steps run and their parameters by editing the **configuration section** at the top of `main.py`:

```python
# --- Step toggles (set to False to skip a step) ---
SYNC_TIDAL = True
RESEARCH_ARTISTS = True
VERIFY_CONCERTS = True
GENERATE_REPORT = True

# --- Research settings ---
RESEARCH_LOCAL = False          # Use Ollama instead of Anthropic API
RESEARCH_LIMIT = None           # Max artists to process (None = all)
RESEARCH_NAMES = None           # e.g. "Radiohead,Blur" to force-research specific artists
RESEARCH_NO_PAGE_PARSER = False # Disable the page parser tool

# --- Verification settings ---
VERIFY_LIMIT = None             # Max concerts to verify (None = all)
VERIFY_SKIP_PAST = True         # Skip concerts with dates before today
VERIFY_NO_SEARCH = False        # Only Tier 1 (re-fetch), no search fallback
```

### Run individual steps

Each step can also be run independently:

```bash
# Step 1: Sync favorites from Tidal
python main.py  # with only SYNC_TIDAL = True

# Step 2: Research artists
python research_artists.py
python research_artists.py --local                    # use Ollama
python research_artists.py --names "Radiohead,Blur"   # specific artists
python research_artists.py --limit 10                 # test run

# Step 3: Verify concerts
python verify_concerts.py
python verify_concerts.py --limit 5 --skip-past       # test run

# Step 4: Generate report (unverified)
python generate_report.py
```

---

## Pipeline steps

### Step 1 — Sync favorites from Tidal

Fetches your full list of favorite artists (all pages) and their bios from Tidal. Results are saved to `favorite_artists.json`. Re-running updates the Tidal-sourced fields (`name`, `bio`) while preserving any custom metadata.

### Step 2 — Research activity & find concerts

For each artist not yet checked (or not checked in the last 30 days), an LLM agent:
1. Reads the stored bio
2. Looks up the artist on MusicBrainz if the bio is ambiguous
3. Does a targeted web search if needed
4. Optionally fetches full web pages for detailed concert extraction (page parser)
5. Determines active status and searches for Zürich-area concerts

By default uses Claude Sonnet via the Anthropic API. Set `RESEARCH_LOCAL = True` (or `--local`) to run via Ollama.

Results are saved to `favorite_artists.json` (status fields) and `upcoming_concerts.json` (concert events).

**On subsequent runs**, already-researched artists are skipped:
- Checked within the last 30 days → skipped
- Permanently inactive (deceased / definitively disbanded) → skipped forever

### Step 3 — Verify concerts

Tiered verification against source URLs and web searches:
- **Tier 1**: Re-fetch the original source URL and compare
- **Tier 2**: Targeted web search if source is unfetchable or inconclusive
- **Tier 3**: Mark as unverified if no signal

Produces `verified_concerts.json` and `concert_report_verified.html`.

### Step 4 — Generate report

Generates `concert_report.html` from the raw (unverified) concert data.

---

## Data files

| File | Description |
|------|-------------|
| `favorite_artists.json` | Dict keyed by Tidal artist ID with name, bio, active status |
| `upcoming_concerts.json` | Flat list of found concerts near Zürich |
| `verified_concerts.json` | Concerts with verification status and confidence |
| `venues.json` | Auto-learned venue → city mappings |
| `concert_report.html` | HTML report from raw concert data |
| `concert_report_verified.html` | HTML report from verified concert data |
| `verification_log.json` | Structured log of verification decisions |
| `failed_fetches.json` | Log of URLs that couldn't be fetched |

### `favorite_artists.json`

Each entry:

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

You can add any extra fields directly (e.g. `"seen_live": true`) — they are preserved across syncs and research runs.

### `upcoming_concerts.json`

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

---

## Recommended update schedule

| Step | How often | Why |
|------|-----------|-----|
| Tidal sync | Monthly | Picks up newly favorited artists and refreshed bios |
| Research | Monthly | 30-day cache expires; finds new tour announcements |
| Verification | After research | Confirms concert data is still accurate |

Run the full pipeline with a single command:

```bash
python main.py
```
