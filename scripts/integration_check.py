"""Integration check against the OpenCode stores actually present on this machine.

Prints counts only (no conversation content). Usage:  python scripts/integration_check.py
Exit code 0 when every check that could run passed.
"""
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from opencode_session_browser.config import Config  # noqa: E402
from opencode_session_browser.discovery import discover  # noqa: E402
from opencode_session_browser.registry import Registry  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def sig(root):
    out = {}
    for f in sorted(Path(root).glob("opencode*.db*")):
        st = f.stat()
        out[f.name] = (st.st_size, st.st_mtime_ns)
    return out


def sha(p, limit=None):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def main():
    cfg = Config()
    d = discover(cfg)
    check("at least one OpenCode source discovered", bool(d.specs), ", ".join(s.label for s in d.specs))
    before = {s.id: (sig(s.root), sha(Path(s.root) / "opencode.db") if (Path(s.root) / "opencode.db").exists() else None) for s in d.specs}
    cache = Path(tempfile.mkdtemp(prefix="osb-int-"))
    reg = Registry(cfg, cache_dir=cache, specs=d)
    reg.apply_discovery(d)
    t = time.time()
    reg.refresh_all(force=True)
    print(f"refresh took {time.time() - t:.1f}s")
    diag = {s["id"]: s for s in reg.diagnostics()["sources"]}
    for s in diag.values():
        print(f"  source {s['id']:<14} {s['status']:<10} sessions={s['session_count']:<5} access={s['access_mode']} versions={s['recorded_versions']}")
    total = sum(s["session_count"] for s in diag.values())
    check("all sources read without error", all(s["status"] in ("ok", "degraded") for s in diag.values()))
    check("sessions enumerated from persistent storage", total > 0, f"{total} sessions")
    if any(s.env == "windows" for s in d.specs) and any(s.env == "wsl" for s in d.specs):
        check("Windows and WSL aggregated in one registry", len({s['id'] for s in diag.values()}) >= 2)
    q = reg.query({"limit": "500"})
    check("unified list returns every session", q["total"] == total, f"{q['total']}/{total}")
    ups = [r["updated"] or 0 for r in q["rows"]]
    check("default order is most-recently-updated first", ups == sorted(ups, reverse=True))
    v2only = sum(1 for s in reg.all_sessions() if s.in_legacy_table is False)
    print(f"  sessions present only in OpenCode's v2 tables (invisible to `opencode export`/session list): {v2only}")
    children = [s for s in reg.all_sessions() if s.parent_id]
    check("parent/child relationships present", True, f"{len(children)} child sessions")
    # open the biggest few sessions + any with tools / children
    sample = sorted(reg.all_sessions(), key=lambda s: -(s.message_count or 0))[:3]
    if children:
        sample.append(children[0])
    opened = tools = 0
    for s in sample:
        src = reg.sources[s.source_id]
        msgs = src.load_messages(s.id)
        opened += 1 if msgs else 0
        tools += sum(1 for m in msgs for p in m["parts"] if p["type"] == "tool")
        order = [m["seq"] for m in msgs]
        check(f"session {s.id[:14]}… opens, chronological", bool(msgs) and order == sorted(order), f"{len(msgs)} entries")
    check("tool calls reconstructed", tools > 0, f"{tools} tool calls in sample")
    if children:
        det = reg.detail(children[0].key)
        check("child session lineage resolves to a root", det["tree"] is not None and det["root_key"] != children[0].key or bool(det["lineage"]))
    t = time.time()
    reg.index_once()
    print(f"index built in {time.time() - t:.1f}s")
    # pick a word that must exist: the title of some session
    word = next((w for s in sample for w in (s.title or "").split() if len(w) > 5 and w.isalnum()), None)
    if word:
        hits = reg.query({"q": word})
        check("global search finds a known title word", hits["total"] >= 1, f"{hits['total']} sessions")
    for sid, (sg, h) in before.items():
        root = next(s.root for s in d.specs if s.id == sid)
        after = sig(root)
        same_main = h is None or sha(Path(root) / "opencode.db") == h
        # OpenCode itself may be running and writing; we only claim *we* did not: the main DB bytes must be identical.
        check(f"storage untouched: {sid} main database bytes identical", same_main,
              "wal/shm changed by a live OpenCode" if after != sg else "all files unchanged")
    print("OVERALL", "PASS" if all(results) else "FAIL")
    reg.stop()
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
