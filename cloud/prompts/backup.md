# Weekly backup pass (Mondays, a few hours after the sweep)

Goal: read the venue calendars the weekly sweep couldn't reach because the hourly page-read limit ran out, and add what they show.

Follow `cloud/prompts/common.md` for setup and finishing, with these changes:
- Skip the Tidal sync; the sweep already ran it.
- Read `cloud/runs/pending.json`. If it is missing or `[]`, post nothing, make no commit, and end the run with `no_reply_needed`.
- Read only the venues named there, using `cloud/prompts/weekly.md` steps 1 to 5 (calendar extraction, match, verify, recheck tracked shows at those venues, findings). Keep to about 12 page reads per agent so the hourly limit holds.
- Merge with `--kind backup`. Then set `cloud/runs/pending.json` to the venues still unread (normally `[]`) and commit with the message `concerts: backup run <YYYY-MM-DD>`.
- Post a `reply` only if something is new or changed, in the same shape as the weekly message. If nothing changed, end with `no_reply_needed`.
