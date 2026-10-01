# What was observed on the OpenCode installations this was built against

Everything below was observed locally (2026-10-01), not taken from documentation. Only counts and
schema are recorded here; no conversation content.

## Installations

| Environment | Executable | Version | Data directory | Format |
|---|---|---|---|---|
| Native Windows | `C:\Users\<user>\AppData\Roaming\npm\opencode` (npm global) | 1.18.34 | `C:\Users\<user>\.local\share\opencode` (`opencode db path` agrees) | SQLite `opencode.db` (+ `-wal`, `-shm`), 737 MB, WAL journal |
| WSL (Ubuntu, WSL2) | the *Windows* binary above, reached through WSL interop (`which opencode` → `/mnt/c/...`) | 1.18.34 | `~/.local/share/opencode` (own DB, own `auth.json`, `log/`, `repos/`) | SQLite `opencode.db`, WAL |

* There is no `storage/` JSON tree on either side (that was the pre-SQLite layout); `snapshot/`, `tool-output/`,
  `worktree/`, `shell/`, `log/` and `repos/` directories exist next to the DB.
* `opencode db path` run from WSL prints the *Windows* path, because that is the Windows binary. The WSL data
  directory is only used by something running inside WSL with the WSL `HOME`.
* Sessions recorded in the Windows DB were written by several OpenCode versions: 1.18.31 – 1.18.34 and 2.0.11 – 2.0.18.

## Three session generations can coexist in one database

| Generation | Tables | Seen where |
|---|---|---|
| legacy | `session`, `message`, `part` (JSON `data` columns; parts typed `text`, `reasoning`, `tool`, `step-start`, `step-finish`, `patch`, `file`) | Windows (138 sessions) |
| v2 | `session_v2`, `session_message` (typed rows: `user`, `assistant`, `system`, `synthetic`, `compaction`, `idle`, `agent-switched`, `model-switched`, `location-switched`; ordered by `seq`; assistant rows hold a `content[]` array of `text` / `reasoning` / `tool` parts) | Windows (358 → 360 sessions) |
| newer `session` + `session_message` | `session` plus v2 messages, no `session_v2`; `session_input`, `session_context_epoch` tables | WSL |

On Windows `session_v2` is a **superset**: all 138 legacy sessions are also in `session_v2`/`session_message`
(message and tool counts match 1:1), and **220 sessions exist only in v2**. This is the "sessions that OpenCode
does not show" case:

* `opencode session list --format json` returned **41** sessions (the `global` project of the current directory); the
  database holds **360**.
* `opencode export <id>` on a v2-only session answers `Session not found: ses_...`, although the session is fully
  stored in `session_v2` / `session_message`.

## Records

* `session_v2`/`session`: id (`ses_...`), `project_id`, `parent_id` (sub-agent hierarchy; 101 child sessions on Windows),
  `fork_session_id`, `slug`, `directory`, `path`, `title`, `version`, `agent`, `model` (JSON `{id, providerID, variant}`),
  `cost`, `tokens_*`, `time_created`/`time_updated`/`time_archived` (epoch **ms**), and in v2 `time_idle`, `idle_outcome`
  (`succeeded` / `failed` / `interrupted`), `time_suspended`, `time_viewed`.
* `project`: `id`, `worktree`, `name`. Many sessions belong to the pseudo project `global` (worktree `/`), so the
  *session directory* is the useful project path.
* Tool parts (v2): `{type:"tool", id, name, state:{status: completed|error, input, content:[{type:"text",text}], error, metadata}, time:{created, ran, completed}}`;
  legacy: `{type:"tool", tool, callID, state:{status, input, output, metadata, title, time:{start,end}}}`.
  Shell-like tools are named `shell`, `bash` or `execute`, with `input.command`.
* Sub-agent calls: tool `task`/`subagent`; the result text carries `<task id="ses_..." ...>` which links the call to the child session.
* Errors: assistant record `error` (`{type:"provider.error", message}` in v2, `{name:"APIError", data:{message,statusCode,...}}` in legacy), tool state `error`.
* Secrets live in `account`, `control_account`, `credential`, `session_share` and `auth.json`. The adapter reads an allow-list of
  tables only and never touches these.
* `event` (70k rows) is an event-sourcing log; it is not needed for reconstruction and is ignored.

## Supported interfaces

* `opencode session list --format json` – project-scoped (cwd) list; used only for comparison.
* `opencode export <id>` – legacy-model sessions only. A directory of such exports can be browsed (`kind: export-dir`).
* `opencode serve` – exposes `GET /session/status` (`{}` when nothing is busy). It is per server instance, reachable from
  Windows `localhost` (not from WSL's `localhost` in NAT mode here), password via `OPENCODE_SERVER_PASSWORD`. It is the only
  authoritative "running" signal, so the browser uses it when a `status_url` is configured and shows `unknown` otherwise.
* WSL → Windows: SQLite cannot use WAL shared memory on `/mnt/c` (9p), a direct read-only open fails with
  `disk I/O error`; Windows → WSL (`\\wsl.localhost\...`) has the same limitation. Hence the snapshot-copy strategy in the README.
