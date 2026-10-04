# Lessons from the local pipeline

Distilled from `../../notes.md` (the March 2026 runs of the original scripts). Apply them in every run.

## Trust order for evidence
1. **The venue's own calendar** is the most authoritative source. If an artist is on it, the show is confirmed. If a venue calendar that should list the show doesn't, treat that as a real negative signal and dig further.
2. **Event-specific pages** come next: `detour.songkick.com/concerts/…`, `bandsintown.com/e/…`, `<venue>.ch/events/<artist>`, petzi.ch event pages.
3. **Artist-level pages** (ticketmaster.com/artist, livenation.com/artist, tour news articles) are weak because they list global dates, and the Swiss one is often missing.
4. **Social media only** (Instagram/Facebook): record it as `tentative` with a note, never as `confirmed`.

## Known traps
- **Misattribution** is the most common error: a venue calendar line naming a different headliner or support act gets credited to your artist. Past examples: "toe" matched a DON'T TRY show at Post Squat, "Pseudonym Prada" matched a Jule X show at Komplex 457. Before recording a show, check that the artist really is the performer on that date.
- **Namesakes and tribute acts.** "X tribute", "plays the music of X" and cover bands are not the artist.
- **Stale pages.** Old tour articles and past festival lineups can show dates from earlier years, so check the year. Anything dated before today is in the past.
- **Sites that block scrapers:** ticketcorner.ch, ticketmaster.ch, ra.co, concertful.com, myswitzerland.com. Use the search snippet or another source instead of retrying.
- `songkick.com/artists/*` returns 410. Use `detour.songkick.com/concerts/*` event pages instead.
- **Festivals** publish their lineups gradually. A festival confirmed for the artist but without a day yet is `festival_pending`. Use the festival's first day as the date and explain in `notes`.

## Statuses for findings
`confirmed` · `tentative` (single weak source, typically social media) · `festival_pending` · `cancelled` (an explicit cancellation of a show we already track).

## Area
German-speaking Switzerland: the cantons ZH, BE, LU, UR, SZ, OW, NW, GL, ZG, SO, BS, BL, SH, AR, AI, SG, GR, AG, TG, plus German-speaking Valais (Oberwallis: Gampel, Zermatt, Visp, Brig) and the bilingual cities Biel/Bienne and Fribourg/Freiburg (Düdingen included). Out of scope: Geneva, Vaud, Neuchâtel, Jura, Ticino, French-speaking Valais.

## Horizon
From today to six months ahead (183 days). Ignore anything later. It will come into range in a later run.
