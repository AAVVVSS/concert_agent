# Weekly sweep (Mondays)

Goal: find favorite artists on the calendars of German-speaking Swiss venues and open airs within the next six months, and catch changes to concerts we already track. This is cheap and broad, with no per-artist searching.

Follow `cloud/prompts/common.md` for setup and finishing. In between:

1. **Read the calendars.** For every entry in `cloud/data/venues.json`, fetch its `url`, following the program/events page and its pagination up to the horizon. Extract every event as `{"title", "date": "YYYY-MM-DD", "venue", "city", "url"}`, where `url` is the event's own link if it has one. For open airs, extract the lineup and use the festival's dates (the first day when no day is given per act). Aggregators (petzi, stadtkonzerte) list many venues, so use each event's real venue and city.
   - If an entry has `url: null`, or its page has no usable events, find the official program page with WebSearch. Update `url` (and a short `notes` like "JS calendar, use petzi" when useful) in `venues.json` so the next run starts better.
   - If you notice an important venue missing, add it. Keep the file's shape.
   Write everything to `cloud/runs/tmp/events.json`.
2. **Match.** Run `python3 cloud/concerts.py match cloud/runs/tmp/events.json > cloud/runs/tmp/matches.json`. Matching works on whole words, but it still produces false positives such as namesakes, tributes and support slots.
3. **Verify each match** against the lessons: right artist, right date, inside the area and horizon. Use the event page when the calendar line is ambiguous. Keep only real ones.
4. **Recheck tracked shows.** For every concert in `cloud/data/concerts.json` dated between today and the horizon whose venue calendar you read this run: if it's still listed, include it again unchanged. If it's gone, look for a cancellation or a move and record `cancelled`, or the new date or venue with `replaces` set to the old entry's `id`. When unsure, leave it out of the findings, which leaves it untouched.
5. Write the verified list to `cloud/runs/tmp/findings.json`, one object per show:
   `{"artist_id", "artist_name", "date", "venue", "city", "kind": "concert"|"openair", "url", "status", "notes", "replaces"?}`
