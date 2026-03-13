# Concert Agent - Technical Notes

## Page Fetching & Extraction Pipeline

### fetch_page
- Uses a shared `requests.Session` with full Chrome User-Agent, `Accept`, `Accept-Language`, and `Accept-Encoding` headers.
- Automatic retries (2 attempts, 0.5s backoff) on 500/502/503/504 via `urllib3.util.retry.Retry`.
- `Accept-Encoding` must NOT include `br` (brotli) — Python `requests` doesn't decode brotli by default, causing binary garbage (discovered with thehall.ch).

### html_to_text
- Extracts `<script type="application/ld+json">` blocks BEFORE stripping all `<script>` tags. These JSON-LD blocks often contain structured `MusicEvent`, `Organization`, or `Place` data with venue addresses.
- JSON-LD blocks are prepended to the output as `[STRUCTURED DATA]` sections.
- If a venue map dict is passed in, venue-city pairs are automatically extracted from JSON-LD `Organization`/`Place`/`MusicVenue`/`EventVenue` types with `address.addressLocality`.
- Also checks `location` nested inside Event types for venue info.

### parse_concert_page
- Skips known-unsupported domains (instagram.com, facebook.com, fb.com) immediately with a clear message.
- On failure, appends actionable domain-specific hints to the error message (e.g., "Ticketmaster CH blocks scraping. Use the search snippet instead.").
- Injects the venue map into the extraction prompt so the parser LLM can infer cities from known venue names.

## Domain-Specific Issues

### Ticketmaster CH (ticketmaster.ch)
- Returns 401 Unauthorized — Cloudflare protection blocks automated fetches even with full browser headers.
- Tavily extract also fails.
- **Workaround**: The search snippet from Tavily often contains the date/venue. The SYSTEM_PROMPT and web_search annotations tell the agent to use the snippet directly instead of calling parse_concert_page.

### Songkick
- `songkick.com/artists/*` pages return 410 Gone (domain migrated).
- Concert-specific pages on `detour.songkick.com/concerts/*` DO work and contain JSON-LD `MusicEvent` data with full venue/date info.
- The SYSTEM_PROMPT, tool descriptions, and web_search annotations all warn the agent about this.

### Instagram / Facebook
- Fully JS-rendered, require authentication. Direct fetch returns ~9 chars ("Instagram").
- Tavily extract also fails.
- Detected early via `UNSUPPORTED_DOMAINS` set and `_is_unsupported_domain()` check.
- Skipped in both `parse_concert_page` (returns explanation) and `test_pages.py` Phase 1 (avoids wasting time).

### Venue Pages (thehall.ch, dampfzentrale.ch, etc.)
- Work well with direct fetch.
- Often contain JSON-LD with Organization schema (venue name + address).
- Main challenge: venue pages often list ALL events, not just the target artist. The parser LLM needs to find the specific artist within a full event calendar.
- The venue map is critical for these pages — without it, the parser LLM may refuse to report a concert because the page doesn't explicitly name the city (e.g., The Hall's page says "THE HALL" but not "Dübendorf" or "Zürich").

### Additional Unfetchable Domains (added to _UNFETCHABLE_DOMAINS)
- **ra.co** — Returns 403 Forbidden. Resident Advisor blocks automated fetches.
- **myswitzerland.com** — Returns 406 Not Acceptable.
- **ticketcorner.ch** — Connection timeouts (46+ seconds). Swiss ticket platform with aggressive bot protection.
- **concertful.com** — Returns 403 Forbidden.
- These were identified from the verification_log.json analysis (March 2026 run) where they had 0% success rate with ≥1 attempt each.

## Venue Mapping System (venues.json)

### Three sources of data (combined)
1. **Seed from existing concert data**: On first run, reads `upcoming_concerts.json` and extracts all venue+city pairs. Produced 53 venues from 88 concerts.
2. **JSON-LD extraction**: When fetching venue pages, automatically extracts venue name + city from structured data.
3. **Auto-grow on concert save**: When a new concert is saved with a venue+city not yet in the map, it's added automatically.

### Venue name normalization
- Strips room/sub-venue suffixes: "Kaufleuten Klubsaal" -> "Kaufleuten", "X-TRA House of Music" -> "X-Tra".
- Suffix list: " House of Music", " Musikcafe", " Klubsaal", " Klub", " Club", " Saal", " Stage", " Arena".
- Short names (<=4 chars) go uppercase: "KKL" -> "KKL", "EXIL" -> "EXIL".
- Longer names get title-cased: "rote fabrik aktionshalle" -> "Rote Fabrik Aktionshalle".
- Some duplicates remain for edge cases with parenthetical sub-venues (e.g., "Komplex 457" and "Komplex Klub (Komplex 457)") but they point to the same city, so no harm.

### Prompt injection
- The venue map is formatted as `"Venue1=City1, Venue2=City2, ..."` and injected into:
  - The SYSTEM_PROMPT (via `build_system_prompt()` template) for the main agent.
  - The extraction prompt in `parse_concert_page` for the parser LLM.
- `_SYSTEM_PROMPT_TEMPLATE` uses `{venue_map}` placeholder with double-braced JSON examples (`{{`, `}}`) to avoid format string conflicts.

## Agent Guidance (SYSTEM_PROMPT)

### URL Reliability Guide
The SYSTEM_PROMPT now includes explicit guidance on which domains to avoid with `parse_concert_page`:
- instagram.com, facebook.com — rejected at code level.
- ticketmaster.ch — 401, use snippet instead.
- songkick.com/artists/* — 410 Gone, use detour.songkick.com/concerts/*.

### URL Preference Guide
The SYSTEM_PROMPT explicitly instructs the agent to prefer event-specific URLs over artist-level pages:
- GOOD: `songkick.com/concerts/12345`, `bandsintown.com/e/12345`, `venue-site.ch/events/artist-name`
- BAD: `ticketmaster.com/artist/12345`, `livenation.com/artist/name`, `loudersound.com/news/tour-announcement`
- Reason: Artist-level pages list many concerts globally; the Swiss date may not appear. Event-specific pages are far more useful for verification.

### Failure Handling
The agent is told: if parse_concert_page returns an error, do NOT retry the same URL. Instead use the search snippet directly or try a different search query.

### web_search Annotations
Search results are tagged inline with domain warnings:
- `[blocked - use snippet only]` for ticketmaster.ch.
- `[410 Gone - try detour.songkick.com]` for songkick.com/artists/*.
- `[unsupported - skip]` for instagram.com, facebook.com.
This lets the agent see the warning before deciding to call parse_concert_page.

### Tool Descriptions
The `parse_concert_page` tool description in both Anthropic and OpenAI formats lists which domains to use and which to avoid.

## Test Harness (test_pages.py)

### Three phases
1. **Phase 1** — Fetch + HTML extraction (no LLM). Tests `fetch_page` + `html_to_text`. Reports HTML size, extracted text size, JSON-LD blocks found, and relevant content lines. Unsupported domains are skipped immediately.
2. **Phase 2** — Tavily extract fallback. Tests Tavily on both hard failures AND thin-content pages (fetched OK but <50 chars). Unsupported domains are skipped.
3. **Phase 3** — Full Ollama extraction. Tests `parse_concert_page` with the venue map loaded. Requires Ollama running with qwen3:8b.

### Test cases
Six URLs covering diverse sources: Songkick event page, Ticketmaster CH, Dampfzentrale venue, The Hall venue, Instagram, Songkick artist page.

### Key finding
The test script must pass `venues` to `parse_concert_page` — without it, the parser LLM lacks context to infer cities from venue names and produces false negatives (e.g., Beirut at The Hall).

## Current Success Rate

| Artist | Phase 1 | Phase 3 (LLM) | Notes |
|--------|---------|---------------|-------|
| A Perfect Circle | OK + JSON-LD MusicEvent | Extracts date, venue, city | detour.songkick.com works |
| bar italia | OK | Extracts date, venue, city | Dampfzentrale venue site |
| Beirut | OK + JSON-LD | Extracts date, venue, city (with venue map) | Required venue map to resolve city |
| Altin Gun | 401 Unauthorized | Returns error + hint | Ticketmaster CH blocks scraping |
| Blonde Redhead | Skipped (unsupported) | Returns skip message | Instagram requires JS/auth |
| Polyphia | 410 Gone | Returns error + hint | Old Songkick domain is dead |

3/6 fully working, 3/6 return clear error messages with actionable hints. The remaining 3 are external limitations (Ticketmaster auth, Songkick domain migration, Instagram JS rendering) that the main agent handles by using search snippets directly or trying alternative sources.

---

## Verification Pipeline (verify_concerts.py)

### Tiered Architecture

The verification pipeline uses a multi-tier evidence gathering strategy before sending evidence to an LLM (Claude Sonnet) for comparison against stored concert data.

```
Tier 1:   Re-fetch source URL directly
Tier 1.5: Fetch venue's event calendar page (if tier1 has no evidence)
Tier 2:   Tavily web search with multi-query strategy
Fallback: If tier1 succeeded but LLM said "unverified", try tier2 as supplemental evidence
```

### Tier 1 — Source URL Re-fetch
- Re-fetches the URL stored during research and extracts text via `html_to_text`.
- Skips URLs classified as "unfetchable" (Instagram, Facebook, ra.co, ticketcorner.ch, etc.).
- Records detailed metadata: HTTP status, response headers, redirect chain, JSON-LD types, HTML size.
- Pages with <50 chars of extracted text are treated as "empty_response" failures.

### Tier 1.5 — Venue Calendar Check (new)
- When tier1 yields no evidence, looks up the venue name in `_VENUE_CALENDARS` mapping.
- Fetches the venue's event calendar page directly and checks if the artist name appears in the text.
- Provides an alternative evidence path that bypasses the original source URL entirely.
- Currently covers 22 Swiss venues with known working calendar URLs.
- Falls through silently if the venue isn't in the mapping or the artist isn't found on the calendar.

### Tier 2 — Tavily Web Search
- Uses a **multi-query strategy** (`_build_search_queries`), trying up to 3 queries in order:
  1. Site-targeted: `"artist" city YYYY-MM site:songkick.com OR site:bandsintown.com OR site:setlist.fm`
  2. Generic: `"artist" concert "venue" YYYY-MM Switzerland`
  3. Broad fallback: `"artist" concert "city" 2026`
- Stops at the first query that returns results.
- 0.5s pause between queries, 1.0s pause after completion for rate limiting.

### Tier 2 Fallback
- When tier1 succeeds but the LLM says "unverified" (common with generic artist pages that don't mention the Swiss date), a supplemental tier2 search fires.
- Tier1 + tier2 evidence are combined with a separator and sent to the LLM together.
- Evidence source recorded as `"source_refetch+web_search"`.

### LLM Comparison
- Uses Claude Sonnet (`claude-sonnet-4-6`) with temperature 0.0 and max_tokens 256.
- Prompt provides: stored artist, date, venue, city, country, URL + evidence text (capped at 8000 chars).
- Returns JSON with: status, confidence (high/medium/low), new_date/venue/city if changed, notes.
- **Past-date guard**: If the LLM reports `date_changed` with a date in the past, status is overridden to `"past"`. This catches stale data (e.g., Filter's "new date" was 2024-03-19).

### Verification Statuses

| Status | Meaning |
|--------|---------|
| `confirmed` | Evidence directly confirms same artist, date, venue |
| `date_changed` | Concert exists but date has changed (includes new_date) |
| `venue_changed` | Concert exists but venue/city has changed |
| `details_changed` | Multiple fields changed |
| `cancelled` | Evidence explicitly says concert is cancelled |
| `past` | Concert date is in the past |
| `tentative` | Source was social media only; no corroborating evidence found |
| `festival_pending` | Festival confirmed but per-artist lineup may not be published yet |
| `unverified` | No evidence obtained or evidence doesn't mention the concert |

### Post-LLM Status Overrides

After the LLM comparison, two additional overrides are applied:

1. **Festival detection**: If status is "unverified" and the venue matches a known Swiss festival keyword (via `_FESTIVAL_KEYWORDS` dict covering ~17 festivals), and the event is >60 days away, status is set to `"festival_pending"`. Rationale: festivals announce lineups gradually; being unverified before lineup publication is expected.

2. **Social media tagging**: If status is "unverified" and the concert was tagged with `source_quality: "social_media_only"` during research (meaning the original URL was Instagram/Facebook and no better URL was found), status is set to `"tentative"`. This distinguishes "can't verify because source is Instagram" from "actively suspicious."

---

## Research Pipeline Improvements (research_artists.py)

### URL Upgrade for Social Media Sources
- When a concert's source URL is from Instagram/Facebook (`_is_unsupported_domain`), a post-processing step (`_try_upgrade_url`) searches for a better URL.
- Searches: `"artist" concert city YYYY-MM site:songkick.com OR site:bandsintown.com OR site:jambase.com`
- Accepts URLs from preferred domains (songkick, bandsintown, jambase, setlist.fm, livenation, ticketmaster) or any non-social-media domain.
- If no better URL is found, the concert is tagged with `source_quality: "social_media_only"` for downstream handling.

### Cross-Validation Against Hallucination
- After the research agent returns concerts, each entry is cross-validated via `_validate_concert`.
- Searches Tavily for `"artist_name" "venue_name"` (both exact-quoted).
- If results exist but none mention the artist name in title or content, the concert is **dropped** as likely misattributed.
- Fails open: if no search results at all (artist is too niche) or on API error, the concert is kept.
- **Why this matters**: The research agent sometimes misattributes venue calendar events to the wrong artist. For example, searching for "toe" returns Post Squat events that are actually by DON'T TRY, or "Pseudonym Prada" at Komplex 457 is actually a Jule X show. The cross-validation catches these.

---

## Verification Success Rate Analysis (March 2026 Run)

### Baseline Results (before improvements)
- 88 concerts total
- Confirmed: 37 (42%), Unverified: 25 (28%), Past: 15 (17%), Date/Venue/Details Changed: 11 (13%)
- Success rate (excluding past): 48/73 = 66% actionable verdicts

### Root Cause Breakdown of 25 Unverified Entries

**Category A: Instagram/Facebook source URLs → weak Tavily results (14 entries, 56%)**
- Artists: Blonde Redhead, CHVRCHES, Dolphin Love, Foxwarren, Molchat Doma, Princess Nokia, Pseudonym Prada, Ravyn Lenae, Russian Circles, The Big Moon (x2), Tinariwen, toe, Unwound, Wisp
- What happens: Research stored Instagram/Facebook URL → unfetchable → Tavily search returns generic results → LLM correctly says "unverified"
- Root cause: Upstream research agent saves social media URLs as primary source

**Category B: Tier 1 fetched OK but page doesn't mention the specific concert (10 entries, 40%)**
- Sub-patterns:
  - B1: Source URL is artist-level page, not event-specific (Baby Keem, JID on Ticketmaster.com; Phantogram on LiveNation; Mannequin Pussy on Epitaph; Polyphia on Loudersound)
  - B2: Festival/archive page without current lineup (Jule X on JamBase, TEKE::TEKE on OpenAirGuide)
  - B3: HTTP errors on fetchable-classified domains (Flying Lotus on ra.co → 403, Moby on myswitzerland.com → 406)

### Tier 1 Domain Reliability (from verification_log.json)

| Domain | Success | Fail | Rate |
|--------|---------|------|------|
| www.songkick.com | 10 | 0 | 100% |
| www.bandsintown.com | 6 | 0 | 100% |
| www.livenation.ch | 4 | 0 | 100% |
| kaufleuten.ch | 3 | 0 | 100% |
| detour.songkick.com | 3 | 0 | 100% |
| www.jambase.com | 3 | 0 | 100% |
| www.ticketmaster.com | 2 | 0 | 100% |
| www.livenation.com | 2 | 0 | 100% |
| stadtkonzerte.ch | 2 | 0 | 100% |
| ra.co | 0 | 2 | 0% ** |
| www.ticketcorner.ch | 0 | 2 | 0% ** |
| www.myswitzerland.com | 0 | 1 | 0% ** |
| concertful.com | 0 | 1 | 0% ** |

** = Added to `_UNFETCHABLE_DOMAINS`

### Performance Metrics
- Avg Tier 1 fetch time: 2413ms
- Avg Tier 2 search time: 888ms
- Avg LLM call time: 2873ms
- Total Tavily calls: 27
- Total LLM tokens: 119,897 input / 6,554 output
- Estimated LLM cost: $0.46

### Specific Misattribution Cases Found
- **toe** (Index 71): Stored as Post Squat, Zürich on 2026-04-12. Verification found the actual performer is DON'T TRY (with support from Time To Eat The Dog).
- **Pseudonym Prada** (Index 56): Stored as Komplex 457 on 2026-04-03. Verification found that slot is actually a Jule X concert.
- **Mannequin Pussy** (Index 33): Source URL (epitaph.com) was filtered to show Mamalarky tours, not Mannequin Pussy.

### Stale Date Changes Found
- **Filter** (Index 15): LLM reported date_changed to 2024-03-19 (the past — article was about a 2024 tour).
- **Yet No Yokai** (Index 85): LLM reported date_changed to 2023-06-17 (B-Sides Festival 2023 lineup page).
- These are now caught by the past-date guard and overridden to status "past".

---

## Remaining Hard Limitations

### Truly Unfixable
- **Social-media-only announcements**: Some smaller artists (Wisp, Dolphin Love) genuinely only announce shows on Instagram. No ticketing platform, no venue listing. Can't verify without scraping Instagram (legally/technically problematic). These now get `"tentative"` status instead of `"unverified"`.
- **Festival lineups not yet published**: TEKE::TEKE at Winterthurer Musikfestwochen, Jule X at OpenAir St. Gallen. The festival exists but per-artist schedules come out weeks before the event. These now get `"festival_pending"` status.
- **LLM hallucination in research**: Despite explicit instructions ("Do NOT hallucinate dates", "Do NOT fabricate URLs"), the research agent occasionally conflates search results. The cross-validation step catches the most obvious cases (wrong artist at venue), but subtle errors (correct artist, wrong date) are harder to detect.

### Venue Calendar Limitations
- The `_VENUE_CALENDARS` mapping currently covers 22 venues with known working URLs.
- Some venue sites use JS-rendered calendars (won't work with simple HTTP fetch).
- Calendar URLs may change or go stale — requires periodic maintenance.
- Venue calendars list ALL events, so artist name matching must be case-insensitive and handle variations (e.g., "A$AP Rocky" vs "ASAP Rocky").

### Tavily Search Limitations
- Tavily's `site:` operator doesn't always work reliably — sometimes returns results from other domains.
- Niche artists may have zero results on any search query, causing false "no evidence" outcomes.
- Search results are snippets, not full pages — may lack the specific Swiss date even when the page itself has it.
- Rate limiting (1 request/second) means the multi-query strategy adds 1-2s per concert.

### Cost Considerations
- Each verification run with all tiers costs approximately:
  - ~27-40 Tavily API calls (more with multi-query and fallbacks)
  - ~60-70 Claude Sonnet LLM calls
  - Estimated cost: $0.50-0.80 per run for 88 concerts
- The cross-validation step in research adds ~1 Tavily call per discovered concert.
- The URL upgrade step adds ~1 Tavily call per social-media-sourced concert.
