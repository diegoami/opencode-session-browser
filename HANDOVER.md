# Handover: session tracker (2026-10-13)

## What was asked
The owner wanted a way to **track OpenCode sessions by id**: "do not use the web interface that we have created to
list sessions — just give me fields where I enter session ids and let me track what is happening in a single
session". The reference was `~/projects/isle-wars-archaeology/tools/harness/peek.sh`, which prints
`title · provider/model · directory`, then `tool X [status]: <input>` and `says: <text>` lines.
Several sessions at once must work (they do).

## What was built (all uncommitted — the owner has not asked for a commit yet)
- `src/opencode_session_browser/tracker.py`: `find()` (exact id, `source~id` key, or unique prefix ≥ 8 chars;
  most recently updated copy first), `steps()` (normalised transcript → flat feed: user / says / thinks / system /
  tool / error / event / file), `in_progress()` (only what storage says: a tool still `running`/`pending`, or an
  assistant message without `completed`), `track()` (header + steps; returns `unchanged: true` without steps when the
  client's `fp` equals the message fingerprint).
- `server.py`: `GET /api/track?id=<id>[&fp=<fingerprint>]`, and `/track` serves `static/track.html`.
- `static/track.{html,js,css}`: standalone page, no code shared with the list UI. Id field (several ids separated by
  spaces/commas), one card per session, polling every 2/3/10 s, last 20/50/200/all steps, "show thinking",
  pause/copy/remove per card, click a line to expand, links to sub-sessions and `task` children open new cards.
  Tracked ids live in the URL hash (`/track#ses_a,ses_b`) and in localStorage as a fallback.
- `discovery.py`: also discovers `~/.local/share/<tool>/data/opencode` (the harness sets
  `XDG_DATA_HOME=~/.local/share/harness-opencode/data`), labelled by `<tool>`. Constant `POSIX_SIBLING_PATTERNS`.
- Tests: new `tests/test_tracker.py`; `tests/test_paths_discovery.py` extended with a `<tool>/data/opencode` case.
  `uv run --no-project --with pytest pytest -q` → 47 passed (there is no `.venv`; `python3 -m pytest` is not installed).
- README: new "Session tracker" section; WSL discovery row mentions the tool-specific stores.

## Outside the repo
- Created `~/.config/opencode-session-browser/config.json` with a manual source
  `"WSL: ic2-work" → /home/diego/ic2-work/opencode-data/opencode`. That store is outside `~/.local/share`, so
  auto-discovery can't find it; session `ses_ed48bfc99ffebG1OJfLGa21oFG` (review-a465471ac4, gpt-5.6-luna) lives there.
- `ic2-opencode-2x` and `ic2-opencode-probe` under `/mnt/c/Users/diego/.local/share/` have `data/` dirs but no DB yet;
  once they get one, the existing Windows `<tool>/data/opencode` pattern picks them up.

## Verified
- `/api/track` for `ses_ed4c71bc3ffev2C2vjjrN1Eo6x` (dummy-tracking-test, minimax/MiniMax-M2.7) reproduces peek.sh's output.
- Headless Chromium (`uv run --with playwright`): cards render, unknown id shows "waiting", adding ids updates the hash,
  no page errors. Screenshots hang in this WSL setup (`Page.screenshot` timeout) — inspect DOM text instead.

## Running it
The server is **not running** now (it ran as a background job of the previous session, which ended). Start with:

    cd ~/projects/opencode-session-browser && PYTHONPATH=src python3 -m opencode_session_browser

then open http://127.0.0.1:8780/track. Starting it from a Claude session ties it to that session's lifetime.

## Open / possible next steps
- Commit the work (on `main`; branch first if the owner prefers) once the owner asks.
- "running" comes only from stored data: a killed OpenCode run leaves a tool marked running forever. The authoritative
  alternative is `--status-url` with an `opencode serve` (already supported, shown as a "server: busy" pill).
- Not done: a CLI `track <id>` (tail -f style) — the owner chose the web fields instead.
- Careful with `pkill -f` patterns: a broad one killed the calling shell once; use `pkill -f "python3 -m opencode_session_browser$"`.
