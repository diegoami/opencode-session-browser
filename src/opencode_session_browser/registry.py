"""Aggregates all sources: refresh loop, background indexing, querying, session detail."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import normalize as N
from .config import Config, slug
from .discovery import Discovery, discover
from .index import SearchIndex, parse_query
from .models import SessionSummary
from .paths import (path_kind, basename_any, directory_exists, is_windows, is_wsl, shell_quote_posix, shell_quote_ps, user_cache_dir)
from .source import Source, SourceSpec
from .status import fetch_status

VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+(?:[-+.\w]*)?")


def project_label(s: SessionSummary) -> str:
    if s.project_name:
        return s.project_name
    return basename_any(s.directory) or "(none)"


def probe_version(spec: SourceSpec, cfg: Config) -> dict:
    """Version of the `opencode` executable on PATH *in the environment that owns the source*."""
    exe = cfg.opencode_cmd or "opencode"
    try:
        if spec.env == "wsl" and is_windows():
            cmd = ["wsl.exe", "-d", spec.distro or "", "--", "bash", "-lc", f"{exe} --version"]
            note = "login shell inside the distribution"
        elif spec.env == "windows" and is_wsl():
            cmd = ["cmd.exe", "/c", f"{exe} --version"]
            note = "Windows PATH via interop"
        else:
            path = shutil.which(exe)
            if not path:
                return {"version": None, "note": f"'{exe}' not found on PATH"}
            cmd = [path, "--version"]
            note = "PATH" + (" (Windows binary reached through WSL interop)" if is_wsl() and path.startswith("/mnt/") else "")
            if spec.env == "custom":
                note += "; unverified for custom sources"
        r = subprocess.run(cmd, capture_output=True, timeout=20, stdin=subprocess.DEVNULL, cwd="/mnt/c" if is_wsl() else None)
        out = (r.stdout + r.stderr).decode("utf-8", "replace").replace("\x00", "")
        m = VERSION_RE.findall(out)
        return {"version": m[-1] if m else None, "note": note if m else f"could not parse output: {out.strip()[:120]}"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"version": None, "note": f"{type(exc).__name__}: {exc}"}


class Registry:
    def __init__(self, cfg: Config, cache_dir: Path | None = None, specs: Discovery | None = None):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir or cfg.cache_dir or user_cache_dir())
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sources: dict[str, Source] = {}
        self.discovery: Discovery = specs or Discovery([], [])
        self.index = SearchIndex(self.cache_dir / "index.sqlite")
        self.version = 0                 # bumps whenever anything user-visible changed
        self.started = time.time()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="osb-refresh")
        self._inflight: dict[str, object] = {}
        self._exe_versions: dict[str, tuple[float, dict]] = {}
        self._last_discovery = 0.0
        self.indexing = {"done": 0, "total": 0, "running": False}
        self._threads: list[threading.Thread] = []

    # -- lifecycle ---------------------------------------------------------------------
    def rediscover(self) -> None:
        d = discover(self.cfg)
        self.apply_discovery(d)
        self._last_discovery = time.time()

    def apply_discovery(self, d: Discovery) -> None:
        with self.lock:
            self.discovery = d
            for sp in d.specs:
                if sp.id not in self.sources:
                    self.sources[sp.id] = Source(sp, self.cache_dir)
                else:
                    self.sources[sp.id].spec = sp

    def add_specs(self, specs: list[SourceSpec]) -> None:
        with self.lock:
            for sp in specs:
                self.sources[sp.id] = Source(sp, self.cache_dir)

    def start(self) -> None:
        for fn in (self._refresh_loop, self._index_loop):
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- refresh -----------------------------------------------------------------------
    def refresh_all(self, force: bool = False, wait: bool = True) -> None:
        futs = []
        for src in list(self.sources.values()):
            f = self._inflight.get(src.id)
            if f is not None and not f.done():      # a slow/hung source must not stall the others
                continue
            fut = self._pool.submit(self._refresh_one, src, force)
            self._inflight[src.id] = fut
            futs.append(fut)
        if wait:
            for f in futs:
                f.result()

    def _refresh_one(self, src: Source, force: bool) -> None:
        if src.refresh(force):
            self.version += 1
            self.index.prune(src.id, set(src.sessions))
            self._apply_stats(src)
        self._ensure_exe_version(src)

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.time() - self._last_discovery > 60:
                    self.rediscover()
                self.refresh_all(wait=False)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                pass
            self._stop.wait(max(1.0, float(self.cfg.poll_seconds)))

    def _ensure_exe_version(self, src: Source) -> None:
        hit = self._exe_versions.get(src.id)
        if hit and time.time() - hit[0] < 600:
            return
        self._exe_versions[src.id] = (time.time(), probe_version(src.spec, self.cfg))

    # -- indexing ----------------------------------------------------------------------
    def _apply_stats(self, src: Source) -> None:
        st = self.index.stats(src.id)
        for sid, s in src.sessions.items():
            r = st.get(sid)
            if r:
                s.error_count, s.tool_count = r["error_count"], r["tool_count"]
                s.models = r["models"]

    def index_once(self) -> int:
        """Index every session whose fingerprint changed. Returns number indexed."""
        work = []
        for src in list(self.sources.values()):
            if src.status not in ("ok", "degraded"):
                continue
            for sid, s in src.sessions.items():
                work.append((s.updated or 0, src, sid))
        work.sort(key=lambda w: -w[0])  # most recent first
        todo = []
        for _, src, sid in work:
            try:
                fp = repr((src.sessions[sid].updated, src.sessions[sid].message_count, src.message_fingerprint(sid)))
            except Exception:  # noqa: BLE001
                continue
            if self.index.fingerprint_of(src.id, sid) != fp:
                todo.append((src, sid, fp))
        self.indexing = {"done": 0, "total": len(todo), "running": bool(todo)}
        n = 0
        for src, sid, fp in todo:
            if self._stop.is_set():
                break
            try:
                entries = src.load_messages(sid)
                self.index.put(src.id, sid, fp, entries, N.transcript_stats(entries))
                n += 1
            except Exception:  # noqa: BLE001 - one bad session must not stop indexing
                pass
            self.indexing["done"] += 1
            time.sleep(0.002)
            if n % 25 == 0:
                for s in {t[0] for t in todo}:
                    self._apply_stats(s)
                self.version += 1
        for s in {t[0] for t in todo}:
            self._apply_stats(s)
        if todo:
            self.version += 1
        self.indexing["running"] = False
        return n

    def _index_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.index_once()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(max(2.0, float(self.cfg.poll_seconds)))

    # -- queries -----------------------------------------------------------------------
    def all_sessions(self) -> list[SessionSummary]:
        out: list[SessionSummary] = []
        for src in list(self.sources.values()):
            out.extend(src.sessions.values())
        return out

    def get(self, key: str) -> tuple[Source, SessionSummary] | None:
        sid, _, session = key.partition("~")
        src = self.sources.get(sid)
        if src and session in src.sessions:
            return src, src.sessions[session]
        return None

    def live_statuses(self) -> dict[str, tuple[dict | None, str | None]]:
        out = {}
        for src in self.sources.values():
            if src.spec.status_url:
                out[src.id] = fetch_status(src.spec.status_url, self.cfg.status_password_env)
        return out

    @staticmethod
    def status_of(live, src_id: str, session_id: str) -> tuple[str, str]:
        """(status, basis). 'unknown' unless a live OpenCode server told us otherwise."""
        if src_id not in live:
            return "unknown", "no live status server configured for this source"
        m, err = live[src_id]
        if m is None:
            return "unknown", f"status server unreachable ({err})"
        if session_id in m:
            return m[session_id], "reported by OpenCode server /session/status"
        return "unknown", "not reported busy by the configured OpenCode server (it only lists busy/retrying sessions of its own project)"

    def query(self, p: dict) -> dict:
        sessions = self.all_sessions()
        live = self.live_statuses()
        words = parse_query(p.get("q", ""))
        ins = {k for k in (p.get("in") or "").split(",") if k}
        meta_kinds = not ins or "meta" in ins
        content_kinds = sorted(ins - {"meta"}) or None
        search_content = not ins or bool(content_kinds)
        kinds = content_kinds
        keep = None
        for w in words:
            wl = w.lower()
            hit: set = set()
            if meta_kinds:
                for s in sessions:
                    hay = " ".join(str(x) for x in (s.title, s.id, s.slug, s.directory, s.project_worktree, s.project_name,
                                                     s.agent, s.model, s.provider, s.source_id) if x).lower()
                    if wl in hay:
                        hit.add((s.source_id, s.id))
            if search_content:
                hit |= self.index.matching_sessions(w, kinds)
            keep = hit if keep is None else keep & hit
        srcs = set(filter(None, (p.get("source") or "").split(",")))
        projects = set(filter(None, (p.get("project") or "").split("\x1f")))
        models = set(filter(None, (p.get("model") or "").split(",")))
        since, until = p.get("since"), p.get("until")
        datef = "created" if p.get("field") == "created" else "updated"

        def passes(s: SessionSummary) -> bool:
            if keep is not None and (s.source_id, s.id) not in keep:
                return False
            if srcs and s.source_id not in srcs:
                return False
            if projects and project_label(s) not in projects:
                return False
            if models:
                have = set(s.models) | ({f"{s.provider or '?'}/{s.model}"} if s.model else set())
                if not (have & models):
                    return False
            kind = p.get("kind")
            if kind == "root" and s.parent_id:
                return False
            if kind == "child" and not s.parent_id:
                return False
            arch = p.get("archived")
            if arch == "only" and not s.archived_at:
                return False
            if arch == "exclude" and s.archived_at:
                return False
            if p.get("errors") in ("1", "true", True):
                if not ((s.error_count or 0) > 0 or s.outcome == "failed"):
                    return False
            if p.get("outcome") and (s.outcome or "none") != p["outcome"]:
                return False
            if p.get("status"):
                if self.status_of(live, s.source_id, s.id)[0] != p["status"]:
                    return False
            t = getattr(s, datef)
            if since and (t or 0) < int(since):
                return False
            if until and (t or 0) > int(until):
                return False
            if p.get("v2only") in ("1", "true") and s.in_legacy_table is not False:
                return False
            return True

        matched = [s for s in sessions if passes(s)]
        facets = {"source": {}, "project": {}, "model": {}, "outcome": {}}
        for s in matched:
            facets["source"][s.source_id] = facets["source"].get(s.source_id, 0) + 1
            pl = project_label(s)
            facets["project"][pl] = facets["project"].get(pl, 0) + 1
            for m in (set(s.models) | ({f"{s.provider or '?'}/{s.model}"} if s.model else set())):
                facets["model"][m] = facets["model"].get(m, 0) + 1
            facets["outcome"][s.outcome or "none"] = facets["outcome"].get(s.outcome or "none", 0) + 1
        sort = p.get("sort") or "updated"
        keyf = {
            "updated": lambda s: s.updated or 0, "created": lambda s: s.created or 0,
            "title": lambda s: (s.title or "").lower(), "messages": lambda s: s.message_count or 0,
            "cost": lambda s: s.cost or 0, "project": lambda s: project_label(s).lower(), "source": lambda s: s.source_id,
            "errors": lambda s: s.error_count or 0,
        }.get(sort, lambda s: s.updated or 0)
        matched.sort(key=keyf, reverse=(p.get("order", "desc") != "asc"))
        total = len(matched)
        off, lim = max(0, int(p.get("offset") or 0)), min(500, max(1, int(p.get("limit") or 100)))
        page = matched[off:off + lim]
        rows = []
        for s in page:
            d = s.to_dict()
            d["project_label"] = project_label(s)
            d["status"], d["status_basis"] = self.status_of(live, s.source_id, s.id)
            d["source_label"] = self.sources[s.source_id].spec.label
            if words:
                snips = []
                for w in words[:2]:
                    snips += self.index.snippets(s.source_id, s.id, w, kinds)[:2]
                d["matches"] = snips[:3]
            rows.append(d)
        return {"total": total, "offset": off, "rows": rows, "facets": facets, "version": self.version,
                "indexing": self.indexing}

    # -- detail ------------------------------------------------------------------------
    def lineage(self, src: Source, s: SessionSummary) -> list[dict]:
        chain, cur, seen = [], s, {s.id}
        while cur.parent_id and cur.parent_id in src.sessions and cur.parent_id not in seen:
            cur = src.sessions[cur.parent_id]
            seen.add(cur.id)
            chain.append(self.brief(cur))
        missing = cur.parent_id if cur.parent_id and cur.parent_id not in src.sessions else None
        chain.reverse()
        return chain if not missing else [{"id": missing, "key": None, "title": "(parent not stored)", "missing": True}] + chain

    def tree(self, src: Source, root_id: str, depth: int = 0, seen: set | None = None) -> dict:
        seen = seen if seen is not None else set()
        s = src.sessions[root_id]
        seen.add(root_id)
        kids = sorted((c for c in src.sessions.values() if c.parent_id == root_id and c.id not in seen), key=lambda c: c.created or 0)
        d = self.brief(s)
        d["children"] = [self.tree(src, c.id, depth + 1, seen) for c in kids] if depth < 50 else []
        return d

    @staticmethod
    def brief(s: SessionSummary) -> dict:
        return {"id": s.id, "key": s.key, "title": s.title, "updated": s.updated, "created": s.created,
                "message_count": s.message_count, "error_count": s.error_count, "outcome": s.outcome, "child_count": s.child_count}

    def resume_commands(self, src: Source, s: SessionSummary) -> list[dict]:
        d = s.directory or ""
        sid = s.id
        cmds = []
        if src.spec.env == "windows":
            dw = d.replace("/", "\\") if path_kind(d) == "windows" else d
            cmds.append({"label": "PowerShell", "command": (f"Set-Location -LiteralPath {shell_quote_ps(dw)}; " if d else "") + f"opencode -s {sid}"})
        elif src.spec.env in ("wsl", "linux"):
            cmds.append({"label": "bash (inside the distro)", "command": (f"cd {shell_quote_posix(d)} && " if d else "") + f"opencode -s {sid}"})
            if src.spec.env == "wsl" and src.spec.distro:
                cmds.append({"label": "PowerShell (via wsl.exe)",
                             "command": f"wsl.exe -d {shell_quote_ps(src.spec.distro)} " + (f"--cd {shell_quote_ps(d)} " if d else "") + f"-- opencode -s {sid}"})
        else:
            cmds.append({"label": "shell", "command": f"opencode -s {sid}"})
        return cmds

    def detail(self, key: str) -> dict | None:
        found = self.get(key)
        if not found:
            return None
        src, s = found
        d = s.to_dict()
        live = self.live_statuses()
        d["project_label"] = project_label(s)
        d["status"], d["status_basis"] = self.status_of(live, src.id, s.id)
        d["source_label"] = src.spec.label
        d["source_env"] = src.spec.env
        d["directory_exists"] = directory_exists(s.directory)
        d["lineage"] = self.lineage(src, s)
        root = d["lineage"][0] if d["lineage"] and not d["lineage"][0].get("missing") else None
        d["root_key"] = root["key"] if root else s.key
        d["tree"] = self.tree(src, root["id"] if root else s.id)
        d["children"] = [self.brief(c) for c in sorted((c for c in src.sessions.values() if c.parent_id == s.id), key=lambda c: c.created or 0)]
        d["resume"] = self.resume_commands(src, s)
        d["resume_warning"] = ("This session exists only in OpenCode's v2 tables; `opencode export <id>` reports 'Session not found' for such sessions, so "
                               "resuming it from the CLI may not work." if s.in_legacy_table is False else None)
        d["message_fingerprint"] = repr(src.message_fingerprint(s.id))
        return d

    def diagnostics(self) -> dict:
        srcs = []
        for src in self.sources.values():
            exe = self._exe_versions.get(src.id, (0, {}))[1]
            srcs.append({
                "id": src.id, "label": src.spec.label, "type": src.spec.env, "origin": src.spec.origin, "root": src.spec.root,
                "kind": src.spec.kind, "status": src.status, "error": src.error, "backend_errors": src.backend_errors,
                "session_count": len(src.sessions), "last_refresh": src.last_refresh, "last_attempt": src.last_attempt,
                "access_mode": src.access_mode(), "details": src.details, "recorded_versions": src.versions,
                "executable_version": exe.get("version"), "executable_note": exe.get("note"),
                "status_url": src.spec.status_url, "note": src.spec.note, "distro": src.spec.distro,
                "indexed": self.index.indexed_count(src.id),
            })
        return {"sources": srcs, "wsl_distributions": [d.__dict__ for d in self.discovery.distros],
                "wsl_error": self.discovery.distro_error, "indexing": self.indexing, "version": self.version,
                "cache_dir": str(self.cache_dir), "platform": "windows" if is_windows() else ("wsl" if is_wsl() else "linux"),
                "uptime": time.time() - self.started}
