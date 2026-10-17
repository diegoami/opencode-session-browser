"""Session tracker: find a session by its id in any source and flatten it into a peek-style step feed.

Like `peek.sh`: each tool call with its status and the start of its input, and each message the model wrote.
Read-only, built on the normalised transcript (never on a database schema).
"""
from __future__ import annotations

import json
import time

from .registry import Registry, project_label

TEXT_CLIP = 4000
INPUT_CLIP = 600
SCRATCH_PREFIX = "/tmp/oc-"      # throw-away runs: /tmp/oc-scratch-*, /tmp/oc-allow-*


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"… [{len(s) - n} more chars]"


def _compact(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(v)


def find(reg: Registry, ident: str) -> list[tuple]:
    """(source, summary) pairs for an id, a `source~id` key, or a unique id prefix; most recently updated first."""
    ident = ident.strip()
    if "~" in ident:
        f = reg.get(ident)
        return [f] if f else []
    hits = [(src, src.sessions[ident]) for src in list(reg.sources.values()) if ident in src.sessions]
    if not hits and len(ident) >= 8:
        hits = [(src, s) for src in list(reg.sources.values()) for sid, s in list(src.sessions.items()) if sid.startswith(ident)]
        if len({s.id for _, s in hits}) > 1:      # ambiguous prefix: report nothing rather than the wrong session
            return []
    hits.sort(key=lambda h: -(h[1].updated or 0))
    return hits


def steps(entries: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        kind, t = e.get("kind"), e.get("time")
        if kind == "event":
            text = " ".join(p.get("text") or "" for p in e["parts"]).strip()
            if text:
                out.append({"kind": "event", "time": t, "text": _clip(text, TEXT_CLIP), "error": bool(e.get("error"))})
            continue
        for p in e["parts"]:
            pt = p["type"]
            if pt in ("text", "reasoning"):
                text = (p.get("text") or "").strip()
                if not text:
                    continue
                k = "thinks" if pt == "reasoning" else ("user" if kind == "user" else "system" if p.get("synthetic") or kind != "assistant" else "says")
                out.append({"kind": k, "time": p.get("time") or t, "text": _clip(text, TEXT_CLIP)})
            elif pt == "tool":
                tl = p["tool"]
                out.append({"kind": "tool", "time": tl.get("started") or p.get("time") or t, "tool": tl["name"],
                            "status": tl["status"], "input": _clip(_compact(tl.get("input")), INPUT_CLIP),
                            "error": tl.get("error"), "duration_ms": tl.get("duration_ms"),
                            "child_session_id": tl.get("child_session_id")})
            elif pt == "error":
                out.append({"kind": "error", "time": t, "text": _clip(p.get("error") or "error", TEXT_CLIP)})
            elif pt == "file":
                f = p.get("file") or {}
                out.append({"kind": "file", "time": t, "text": f"{f.get('name') or '?'} ({f.get('mime') or '?'})"})
        if e.get("error") and not any(p["type"] == "error" for p in e["parts"]):
            out.append({"kind": "error", "time": e.get("completed") or t, "text": _clip(str(e["error"]), TEXT_CLIP)})
    return out


def in_progress(entries: list[dict]) -> str | None:
    """What the stored data says is still open (a running tool, an unfinished reply); None if nothing is."""
    for e in reversed(entries):
        if e.get("kind") == "event":
            continue
        for p in e["parts"]:
            if p["type"] == "tool" and p["tool"]["status"] in ("running", "pending"):
                return f"tool {p['tool']['name']} {p['tool']['status']}"
        if e.get("kind") == "assistant" and not e.get("completed") and not e.get("error"):
            return "assistant reply not finished"
        return None
    return None


def track(reg: Registry, ident: str, fp: str | None = None) -> dict:
    hits = find(reg, ident)
    if not hits:
        return {"found": False, "id": ident,
                "message": "not found in any source (yet): new sessions appear within a few seconds of being written"}
    src, s = hits[0]
    live = reg.live_statuses()
    status, basis = reg.status_of(live, src.id, s.id)
    kids = sorted((c for c in src.sessions.values() if c.parent_id == s.id), key=lambda c: c.created or 0)
    parent = src.sessions.get(s.parent_id) if s.parent_id else None
    out = {
        "found": True, "id": s.id, "key": s.key,
        "session": {
            "title": s.title, "provider": s.provider, "model": s.model, "variant": s.variant, "agent": s.agent,
            "directory": s.directory, "project": project_label(s), "source": src.spec.label, "version": s.version,
            "created": s.created, "updated": s.updated, "outcome": s.outcome, "status": status, "status_basis": basis,
            "message_count": s.message_count, "tokens": s.tokens, "cost": s.cost,
            "parent": {"id": s.parent_id, "title": parent.title if parent else None} if s.parent_id else None,
            "children": [{"id": c.id, "title": c.title, "updated": c.updated} for c in kids],
        },
        "also_in": [h[0].spec.label for h in hits[1:]],
    }
    cur = repr(src.message_fingerprint(s.id))
    out["fingerprint"] = cur
    if fp is not None and fp == cur:
        out["unchanged"] = True
        return out
    entries = src.load_messages(s.id)
    out["steps"] = steps(entries)
    out["in_progress"] = in_progress(entries)
    return out


def active(reg: Registry, minutes: float = 10, limit: int = 60, hide_scratch: bool = False) -> dict:
    """Sessions that look active: written to in the last `minutes`, or open in the stored data, or busy on a server."""
    now_ms = time.time() * 1000
    live = reg.live_statuses()
    rows = []
    for src in list(reg.sources.values()):
        for s in list(src.sessions.values()):
            if hide_scratch and (s.directory or "").startswith(SCRATCH_PREFIX):
                continue
            status, basis = reg.status_of(live, src.id, s.id)
            recent = bool(s.updated) and now_ms - s.updated <= minutes * 60_000
            if not (recent or status == "busy"):
                continue
            prog = in_progress(src.load_messages(s.id)) if recent else None
            rows.append({"id": s.id, "key": s.key, "title": s.title, "provider": s.provider, "model": s.model,
                         "directory": s.directory, "project": project_label(s), "source": src.spec.label,
                         "updated": s.updated, "status": status, "status_basis": basis, "in_progress": prog,
                         "parent_id": s.parent_id, "message_count": s.message_count})
    rows.sort(key=lambda r: -(r["updated"] or 0))
    return {"minutes": minutes, "total": len(rows), "sessions": rows[:limit]}
