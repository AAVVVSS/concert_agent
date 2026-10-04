# Shared steps for every scheduled run

You are running unattended, woken by a scheduled routine in the project thread "Concert runs". Earlier runs may be in your context, but always start from the files on main, not from memory. Nobody is watching, so never stop to ask questions. Make the call, note it in the run file, and carry on.

## Before you start
0. Begin with `update_status` ("Weekly concert sweep" or "Monthly deep pass") and keep it current as steps finish.
1. Read `cloud/prompts/lessons.md`. It covers the trust order, known traps, area and horizon.
2. Switch to main and update it: `git fetch origin main && git checkout -B main origin/main`. Alfred decided that run results are committed straight to `main`.
3. Run `uv sync -q`, then sync favorites: `uv run python cloud/tidal_sync.py` (the monthly run adds `--bios`).
   - If it reports that a new Tidal login is needed, keep going with the existing `cloud/data/favorite_artists.json`. Start the final message with `Tidal login expired, please renew TIDAL_REFRESH_TOKEN.`

## Working in parallel
Split large batches across subagents with the Agent tool: about 10 venues or 15 artists per agent, all launched in one message. Give each agent its slice of the list, the full text of `cloud/prompts/lessons.md`, and the exact JSON shape to write to a file under `cloud/runs/tmp/`. Then combine their files yourself. Subagents use WebSearch and WebFetch. Plain `curl` only reaches hosts the environment allows.

## Finishing up
1. Merge verified findings: `python3 cloud/concerts.py merge cloud/runs/tmp/findings.json --kind <weekly|monthly>`. This writes `cloud/runs/<date>-<kind>.json` and prints the phone message.
2. Render the page: `python3 cloud/concerts.py report`.
3. Publish `cloud/report/index.html` with the Artifact tool to the URL in `cloud/config.json` (`report_artifact_url`): first `action: "read"` on that URL, then publish with `url` set to it. Keep the title and don't pass an icon. If `report_artifact_url` is null, publish a new artifact with icon `calendar` and save its URL into `cloud/config.json`.
4. Delete `cloud/runs/tmp/`, then commit everything under `cloud/` with the message `concerts: <kind> run <YYYY-MM-DD>` and `git push origin main`. If the push is rejected because main moved, run `git pull --rebase origin main` and push again.
5. Post one `reply` in the thread, which is what reaches Alfred's phone. It should contain the message the merge printed (prefixed with the Tidal warning if that applied), then a line per new or changed show (artist, date, venue, city, status), then the report link. If nothing is new or changed, post a single line saying so, with the link. Don't narrate the run.
