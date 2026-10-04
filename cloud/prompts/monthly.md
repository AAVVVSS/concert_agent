# Monthly deep pass (first Sunday)

Goal: research the favorite artists whose last check is over 30 days old, or who were never checked, to catch shows the venue sweep can't see (venues not on our list, JS-only calendars, fresh tour announcements). Then re-verify every tracked upcoming concert.

Follow `cloud/prompts/common.md` for setup and finishing (sync with `--bios`). In between:

1. **Pick the artists.** Run `python3 cloud/concerts.py due > cloud/runs/tmp/due.json`. If more than 250 are due, take the first 250 (never-researched ones come first). The rest wait for next month.
2. **Research them in parallel batches.** For each artist:
   - Activity: decide whether they are still active, using the bio in `cloud/data/artist_profiles.json` (or `favorite_artists.json`), MusicBrainz or a quick search. Deceased artists and definitively disbanded bands get `permanently_inactive: true` and no concert search.
   - Shows: search for dates in the area within the horizon, open airs included, e.g. `"<artist>" Zürich 2026`, `"<artist>" Konzert Schweiz`, `"<artist>" tour 2026 Switzerland`, plus Songkick and Bandsintown event pages. Prefer event-specific URLs.
   - Each agent writes `researched-N.json` (`[{"id", "active", "active_reason", "permanently_inactive"}]`) and `candidates-N.json` (finding objects as in the weekly prompt).
3. **Verify the candidates.** Check the venue calendar first, then the event page. Drop anything you can't back up, and mark social-media-only shows `tentative`.
4. **Re-verify tracked shows.** Every concert in `cloud/data/concerts.json` between today and the horizon gets checked: still on, moved (`replaces`), or `cancelled`. Include confirmed ones unchanged in the findings.
5. **Profile new favorites.** Run `python3 cloud/concerts.py profiles-due > cloud/runs/tmp/profiles-due.json`. Write a bio and tags for each artist listed, following `cloud/prompts/profiles.md`, into `cloud/runs/tmp/profiles.json`, then run `python3 cloud/concerts.py profiles-merge cloud/runs/tmp/profiles.json`. Usually only a handful are new.
6. Combine the researched files into `cloud/runs/tmp/researched.json` and run `python3 cloud/concerts.py mark cloud/runs/tmp/researched.json`. Write all verified shows to `cloud/runs/tmp/findings.json`.
