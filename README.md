# OpenCode Session Browser

A **read-only**, local, dependency-free (Python standard library only) browser for every OpenCode session
stored on your machine — native Windows, every WSL distribution, and any extra data root you point it at.
Think *git history browser + chat transcript viewer + log explorer*, not another AI client: it never calls a
model, never resumes a session and never writes to OpenCode's storage.

> **A session does not need to appear in OpenCode Web in order for OpenCode Session Browser to discover it.**
> Discovery reads OpenCode's persistent storage (the SQLite database) directly. On the machine this was built on,
> `opencode session list` showed 41 sessions and `opencode export` could not find 220 stored ones, while the
> database held 360. All 360 are listed. See [docs/OPENCODE_STORAGE.md](docs/OPENCODE_STORAGE.md).

## Features

* One unified, sortable list across Windows + WSL (+ manual roots): updated/created time, source, project, title, id,
  provider/model, status, message count, parent, child count, error indicator. Newest-updated first.
* Global search across title, session id, project/path, user messages, assistant messages, tool names, tool
  commands/inputs, tool results and errors (AND of words, `"quoted phrases"`, Unicode-aware). Filters: source, project,
  provider/model, last outcome, live status, date range, errors only, top-level vs child sessions, archived.
* Transcript viewer: USER / ASSISTANT / SYSTEM / TOOL CALL / TOOL RESULT / ERROR, Markdown + code rendering, collapsible tool
  calls with copy buttons (commands are shown as a copyable shell block), per-message model, tokens (incl. cache), cost,
  duration and finish reason **only where stored**, a per-message *raw* view, unknown record types preserved.
* Arbitrarily nested parent/child (sub-agent) sessions: breadcrumb, session tree, ↑ parent / ↑ root, and `task` tool calls link to their child session.
* Live refresh by low-frequency polling (default every 5 s, only re-reading what changed); the open transcript updates in place without losing scroll position.
* Sources / diagnostics page: per source type, OpenCode version (executable and versions recorded in the sessions), path, detected schema, access mode,
  session count, status, last refresh, errors; plus the installed WSL distributions.
* Export a session as JSON or readable Markdown; copy session id, project path, commands, and a *resume command* (copied only, never executed).
* Privacy: binds to `127.0.0.1`, rejects foreign `Host`/`Origin` headers, no CDN, no telemetry, request bodies/queries are never logged,
  keys that look like secrets are redacted from raw views/exports, credential tables and `auth.json` are never read.

## Install and run

### Development in WSL

```bash
cd ~/projects/opencode-session-browser
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
opencode-session-browser          # http://127.0.0.1:8765
```

From WSL it discovers the WSL store, plus every Windows profile under `/mnt/<drive>/Users/*` that has OpenCode data.

### Native Windows (PowerShell) — discovers Windows *and* all WSL distributions

Keep the checkout on a Windows drive (e.g. `C:\Users\<you>\projects`), not on `\\wsl.localhost\...`.

```powershell
git clone <this repo> $HOME\projects\opencode-session-browser
cd $HOME\projects\opencode-session-browser
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
opencode-session-browser --open
```

Python 3.11+ is required (3.14 tested). Later: `.\.venv\Scripts\opencode-session-browser`.

Options: `--port N`, `--host`, `--source NAME=PATH`, `--status-url SOURCE=URL`, `--no-auto`, `--no-windows`, `--no-wsl`,
`--no-wake-wsl`, `--config FILE`, `--cache-dir DIR`, `--discover` (print sources + session counts as JSON and exit).

## How discovery works

1. **Persistent storage first.** Every data root containing `opencode*.db` (or a legacy `storage/session` tree) is a source. A database is
   inspected at runtime (tables *and* columns), never assumed: `session_v2`, `session`, `session_message`, `message`/`part` are unioned, the
   v2 message model is used when a session has v2 messages and the legacy one otherwise. Sessions are never dropped for missing project
   directories, age, archival, being created by `opencode run`, or being absent from OpenCode Web.
2. **Supported interfaces for enrichment.** Versions come from `opencode --version` in the owning environment; live status comes from
   `GET /session/status` of a running `opencode serve` *if you configure one*. Otherwise status is `unknown` (never guessed from timestamps or
   processes). The last stored idle outcome (succeeded/failed/interrupted) is shown separately.
3. **`opencode export` as compatibility.** Directories of exported JSON can be added as `kind: export-dir`.

Where the roots come from (nothing is hard-coded — no user name, distro or home directory):

| Launched from | Finds |
|---|---|
| Windows | `%XDG_DATA_HOME%\opencode`, `~\.local\share\opencode`, `%LOCALAPPDATA%\opencode`, `%APPDATA%\opencode`; every distro from `wsl.exe -l -v` (system distros skipped) via `\\wsl.localhost\<distro>\home\*` and `\root` → `.local/share/opencode` |
| WSL | `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`; every `/mnt/<drive>/Users/*/{.local/share,AppData/Local,AppData/Roaming}/opencode`. Other distros are *not* reachable from WSL — launch from Windows (or add a manual source). |
| Linux/macOS | `~/.local/share/opencode` |

Stopped WSL distros: reading their filesystem starts them (Windows behaviour). Pass `--no-wake-wsl` / `"wake_wsl": false` to leave them alone (they are then listed as stopped).

### Manual sources

`--source "Backup=D:\old\opencode"` (directory with `opencode.db`, a `.db` file, a `storage/` tree, or with `--kind Backup=export-dir` a folder of `opencode export` files), or a JSON config
(`%APPDATA%\opencode-session-browser\config.json`, `~/.config/opencode-session-browser/config.json`, or `$OPENCODE_SESSION_BROWSER_CONFIG`):

```json
{
  "sources": [
    {"name": "Backup drive", "path": "D:\\old\\opencode", "kind": "auto"},
    {"name": "Exports", "path": "C:\\exports", "kind": "export-dir"}
  ],
  "status_urls": {"Windows": "http://127.0.0.1:4096"},
  "wake_wsl": true,
  "poll_seconds": 5
}
```

### Authoritative live status

Start `opencode serve --port 4096` (optionally with `OPENCODE_SERVER_PASSWORD`, which this tool reads from the environment) and pass
`--status-url Windows=http://127.0.0.1:4096`. Sessions the server reports busy/retrying then show that status. The server only knows its own project
instance, so the absence of a session from its answer still shows `unknown`.

## Read-only guarantees

* SQLite is opened with `file:...?mode=ro` plus `PRAGMA query_only=1`; there is no code path that writes, migrates, indexes, vacuums or checkpoints OpenCode data (a test even attempts a `DELETE` and asserts it is refused, and another hashes the DB before/after a full run).
* Where SQLite WAL cannot work (`/mnt/c` from WSL, `\\wsl.localhost` shares from Windows, network drives), the db and `-wal` files are **copied** into the app cache and the copy is queried; the originals are only read.
  Only changed files are re-copied (a 737 MB DB is copied once; afterwards just the WAL). Two running instances never share a snapshot directory.
* A read-only reader of a WAL database in direct mode may update the `-shm` wal-index read marks (a SQLite requirement); database content is never touched.
* The search index and snapshots live in the app's own cache dir (`%LOCALAPPDATA%\opencode-session-browser\cache` / `~/.cache/opencode-session-browser`). Delete it any time. It contains conversation text — treat it like the OpenCode DB.
* A failing source (missing path, corrupt DB, stopped distro, unsupported schema, hung share) is reported in Sources and never affects the others.

## Architecture

```
src/opencode_session_browser/
  paths.py        Windows/Linux/WSL/UNC path helpers (no OpenCode access)
  discovery.py    finds data roots: native, Windows-from-WSL, WSL-from-Windows, manual
  sqlite_access.py read-only open; snapshot-copy mode for filesystems without WAL support
  adapters/       one class per storage generation behind StorageBackend:
                    sqlite_adapter.py (legacy + v2 + newer, runtime-detected), files_adapter.py (pre-SQLite JSON tree),
                    export_adapter.py (`opencode export` files)
  normalize.py    raw records -> one transcript model (user/assistant/system/event; text/reasoning/tool/file/error/unknown parts)
  source.py       a Source = one data root; refresh, failure isolation, per-session ownership
  index.py        private search index + error/tool statistics, built in the background, incremental
  registry.py     aggregation, filtering/search/sort, hierarchy, status, diagnostics, resume commands
  exporters.py    JSON / Markdown
  server.py       stdlib HTTP server: GET-only JSON API + static UI (POST /api/refresh only re-reads sources)
  static/         vanilla HTML/CSS/JS UI (no framework, no CDN)
```

The UI only consumes the normalised API, never a database schema. A new OpenCode storage version means a new/adjusted adapter.

## Tested versions / formats

* OpenCode 1.18.34 executable (Windows npm install; also used from WSL via interop). Databases containing sessions written by 1.18.31–1.18.34 and 2.0.11–2.0.18.
* Real stores: Windows `opencode.db` (legacy + `session_v2`, 360 sessions) and WSL `opencode.db` (`session` + `session_message`).
* Python 3.12 (WSL) and 3.14 (Windows). 45 automated tests pass on both.
* Synthetic fixtures only (no real conversations are committed): legacy, v2, dual, newer layouts; JSON `storage/` tree; export directories.

## Known limitations

* Only WSL + Windows aggregation from Windows is complete; from WSL, other distros are listed but not read.
* The pre-SQLite JSON tree adapter and the export-directory adapter are verified with synthetic fixtures only (no such installation exists here).
* Images/attachments are listed (name, type, size) but not rendered. `tool-output/` spill files referenced by truncated outputs are not followed.
* Search content per message part is capped at 6000 characters; transcripts clip very large strings to 30 000 characters with a "load full message" button (exports are complete).
* Error counts and the model facet appear once the background index has processed a session (the first run on a large store takes a few seconds to a minute).
* `running` status needs a configured `opencode serve` URL; otherwise `unknown` by design.
* The resume command is a plain `opencode -s <id>` in the session's directory. Sessions that exist only in v2 tables are flagged because `opencode export` cannot find them; whether `opencode -s` can resume them was **not** tested (resuming would call a model).
* OpenCode Web itself was not opened for the comparison; the CLI (`session list`, `export`) was.
* macOS is untested.

## Troubleshooting

* *Nothing found:* run `opencode-session-browser --discover` and read the Sources page. Add a path with `--source`.
* *`disk I/O error` / `database is locked` on a store:* expected for WAL over `/mnt/c` or UNC; the tool switches to snapshot mode by itself (shown under Sources → Access mode).
* *First start is slow on Windows → WSL:* the first snapshot copies the database once; later refreshes copy only the WAL.
* *A WSL distro shows "stopped":* start it, or drop `--no-wake-wsl`.
* *Port in use:* `--port 8780`.
* *Reset everything derived:* delete the cache directory shown on the Sources page.
* *Run the real-machine check:* `python scripts/integration_check.py` (prints counts only).
