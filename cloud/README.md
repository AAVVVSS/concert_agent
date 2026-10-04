# Cloud concert workflow

A scheduled, cloud-hosted version of the concert agent. Claude runs it as two scheduled routines that wake the project thread "Concert runs". Private projects can't start fresh sessions on a schedule, and that thread was started on the environment with Tidal access. The Python scripts in the repo root still work for local runs. This folder is self-contained and doesn't touch them.

| Routine | When (Zurich time) | What it does |
|---|---|---|
| Weekly sweep | Mondays, morning | Syncs Tidal favorites, reads the calendars of the venues and open airs in `data/venues.json`, and matches them against favorites. It also verifies matches and rechecks tracked shows. |
| Monthly deep pass | First Sunday of the month | Syncs favorites (with bios), researches every artist not checked in 30 days, and re-verifies all tracked shows. |

Each run ends by:
- updating `data/concerts.json` (the full state, with history per show),
- writing `runs/<date>-<kind>.json` (what was new or changed),
- republishing the report page, whose fixed URL is in `config.json` (all upcoming shows within six months, new and changed ones flagged),
- committing to `main`, and posting a reply in the thread (which the Claude app pushes to the phone) that lists only the new and changed shows.

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
- **Cloud environment.** Network access set to Custom with `api.tidal.com`, `auth.tidal.com`, and `login.tidal.com` allowed (the device login works without `link.tidal.com`). Venue sites are read through Claude's web fetch, so they need no allowlist.
- **Tidal token.** Run `uv run python -u cloud/tidal_sync.py --login` once in the "Concert runs" thread and approve the printed link. The refresh token is saved to the project's private shared folder (`/mnt/project-files/tidal/tidal_session.json`, never committed). A `TIDAL_REFRESH_TOKEN` environment variable, if set, takes precedence.
- **Changing the schedule or prompts.** The routines only say "follow `cloud/prompts/<kind>.md`", so edits to these files take effect on the next run.

## Local dry run
```bash
TIDAL_REFRESH_TOKEN=... uv run python cloud/tidal_sync.py
python3 cloud/concerts.py due | head
python3 cloud/concerts.py report && open cloud/report/index.html
```
