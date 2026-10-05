# Weekly new-releases check

You are running unattended, woken by a scheduled routine in the project thread "New releases". Nobody is watching, so never stop to ask questions. Start from the files on main, not from memory.

1. Begin with `update_status` ("Weekly new-releases check") and keep it current.
2. Switch to main and update it: `git fetch origin main && git checkout -B main origin/main`. Run results are committed straight to `main`.
3. Run `uv sync -q`, then `uv run python cloud/releases.py`. It reads the favorites from Tidal itself, so `tidal_sync.py` isn't needed. It prints a summary, then the phone message.
   - If it says there is no token or a new login is needed, follow the login steps in `cloud/prompts/common.md`, post the link, and stop there.
4. Publish `cloud/report/releases.html` with the Artifact tool to `releases_artifact_url` in `cloud/config.json`: first `action: "read"` on that URL, then publish with `url` set to it. Keep the title and don't pass an icon.
5. Commit everything under `cloud/` with the message `releases: weekly run <YYYY-MM-DD>` and `git push origin main`. If the push is rejected because main moved, run `git pull --rebase origin main` and push again.
6. Post one `reply` in the thread, which is what reaches Alfred's phone: the phone message the script printed, then the playlist link (if the run file has one) and the report link. If anything failed, add one line saying which artists were skipped. Don't narrate the run.
