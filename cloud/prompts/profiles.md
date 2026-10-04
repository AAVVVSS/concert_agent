# Artist profiles (bio and tags)

`cloud/data/artist_profiles.json` holds one profile per favorite artist, keyed by Tidal artist ID. The report shows the styles under each show and the bio behind an "About" toggle.

## Inputs per artist
- Tidal's bio: `session.artist(id).get_bio()` (via `cloud/tidal_sync.py`'s `get_session` and `clean_bio`), present for about 80% of favorites. It is AllMusic text, so summarise it and never copy it.
- Tidal hints to identify the right artist: a few album titles with years, top tracks and similar artists. Generic names (Pond, Olympia, Rubio, Rocket) often hit a different act with the same name.
- Web search when Tidal has no bio, or when the bio doesn't match the hints.

## Profile format
```json
{
  "name": "Slowdive",
  "bio": "2-4 English sentences in our own words: who, where from, when active, sound, 1-2 key records.",
  "genres": ["rock"],
  "styles": ["shoegaze", "dream pop"],
  "origin": {"country": "GB", "city": "Reading"},
  "active": "1989-1995, 2014-",
  "sources": ["tidal", "https://..."],
  "confidence": "high",
  "note": "only for problems, e.g. 'Tidal profile may be a different artist with the same name'",
  "researched_at": "2026-10-04"
}
```
- `genres`: 1-2 from the fixed list in `concerts.py` (`GENRES`): rock, indie, pop, electronic, hip hop, r&b, soul, jazz, folk, country, blues, metal, punk, latin, reggae, world, ambient, experimental, classical, soundtrack.
- `styles`: 2-5 lowercase established style names (shoegaze, post-punk, neo-psychedelia, reggaeton, trip hop, post-rock, slowcore...). Reuse names already in the file so tags repeat.
- `origin.country`: ISO 3166-1 alpha-2.
- `confidence`: high = Tidal bio or a solid source; medium = pieced together; low = guessed from the hints. Never invent facts.

`profiles-merge` rejects entries without a bio or with genres outside the list.
