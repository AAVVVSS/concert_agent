# Cloud concert workflow

A scheduled, cloud-hosted version of the concert agent. Claude runs it as two Claude Code routines, each firing a fresh cloud session. The Python scripts in the repo root still work for local runs. This folder is self-contained and doesn't touch them.

| Routine | When (Zurich time) | What it does |
|---|---|---|
| Weekly sweep | Mondays, morning | Syncs Tidal favorites, reads the calendars of the venues and open airs in `data/venues.json`, and matches them against favorites. It also verifies matches and rechecks tracked shows. |
| Monthly deep pass | First Sunday of the month | Syncs favorites (with bios), researches every artist not checked in 30 days, and re-verifies all tracked shows. |

Each run ends by:
- updating `data/concerts.json` (the full state, with history per show),
- writing `runs/<date>-<kind>.json` (what was new or changed),
- republishing the report page, whose fixed URL is in `config.json` (all upcoming shows within six months, new and changed ones flagged),
- committing to `main` and sending a phone notification that lists only the new and changed shows.

## Scope
German-speaking Switzerland, plus Biel and Fribourg, from today to six months ahead, open airs included. See `prompts/lessons.md`.

## Files
| Path | Purpose |
|---|---|
| `prompts/weekly.md`, `prompts/monthly.md` | What each routine run does (the routine prompt points here) |
| `prompts/common.md` | Setup, parallelism, publishing, commit and notification steps shared by both |
| `prompts/lessons.md` | Verification rules distilled from `../notes.md` |
| `tidal_sync.py` | Favorites sync using `TIDAL_REFRESH_TOKEN` (no interactive login) |
| `concerts.py` | Deterministic bookkeeping: `due`, `match`, `mark`, `merge`, `report` |
| `data/favorite_artists.json` | Favorites plus research status (custom fields are preserved) |
| `data/venues.json` | Venues, open airs and aggregators to sweep. Runs fix and extend it |
| `data/concerts.json` | All tracked shows |
| `runs/` | One summary per run |
| `report/index.html` | The rendered report that gets published |

## Setup
- **Cloud environment.** Network access set to Custom with `api.tidal.com`, `auth.tidal.com` and `login.tidal.com` allowed. Venue sites are read through Claude's web fetch, so they need no allowlist.
- **`TIDAL_REFRESH_TOKEN`** is an environment variable holding the `refresh_token` from a local `tidal_session.json`. To renew it, run `python main.py` locally with `SYNC_TIDAL = True`, then copy the new value.
- **Changing the schedule or prompts.** The routines only say "follow `cloud/prompts/<kind>.md`", so edits to these files take effect on the next run.

## Local dry run
```bash
TIDAL_REFRESH_TOKEN=... uv run python cloud/tidal_sync.py
python3 cloud/concerts.py due | head
python3 cloud/concerts.py report && open cloud/report/index.html
```
