"""Normalise OpenCode records (any storage generation) into one transcript model.

The UI and exporters only ever see the dictionaries produced here, never raw OpenCode rows.
Unknown record or part types are preserved (``type: "unknown"`` + ``raw``), never dropped.

Transcript entry::

    {id, seq, kind, label, time, completed, agent, provider, model, variant, tokens, cost,
     finish, duration_ms, error, parts: [part], raw}

Part::

    {type: text|reasoning|tool|file|step|patch|event|error|unknown, ...}
"""
from __future__ import annotations

import json
import re
from typing import Any

SECRET_KEY = re.compile(
    r"(api[-_]?key|authorization|secret|password|passwd|access[-_]?token|refresh[-_]?token|bearer|"
    r"cookie|x-api-key|client[-_]?secret)",
    re.I,
)
_B64 = re.compile(r"^[A-Za-z0-9+/=\r\n]+$")
COMMAND_KEYS = ("command", "cmd", "script")
LABELS = {"user": "USER", "assistant": "ASSISTANT", "system": "SYSTEM", "synthetic": "SYSTEM", "compaction": "SYSTEM"}


def loads(text: Any) -> Any:
    """Tolerant JSON decode: malformed / partial records yield None instead of raising."""
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("[redacted]" if SECRET_KEY.search(str(k)) and not isinstance(v, (dict, list)) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def elide_blobs(obj: Any, limit: int = 4096) -> Any:
    """Replace huge base64 payloads (inline images etc.) with a placeholder."""
    if isinstance(obj, dict):
        return {k: elide_blobs(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [elide_blobs(v, limit) for v in obj]
    if isinstance(obj, str) and len(obj) > limit:
        if obj.startswith("data:") or _B64.match(obj[:2000]):
            return f"<{len(obj)} chars of encoded data elided>"
    return obj


def safe_raw(obj: Any) -> Any:
    return redact(elide_blobs(obj))


def to_ms(v: Any) -> int | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f * 1000) if 0 < f < 1e11 else int(f)


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def norm_tokens(t: Any) -> dict | None:
    if not isinstance(t, dict):
        return None
    cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
    out = {
        "input": _num(t.get("input")),
        "output": _num(t.get("output")),
        "reasoning": _num(t.get("reasoning")),
        "cache_read": _num(cache.get("read")),
        "cache_write": _num(cache.get("write")),
        "total": _num(t.get("total")),
    }
    return out if any(v is not None for v in out.values()) else None


def norm_model(m: Any) -> dict:
    """Accept {id, providerID, variant}, {modelID, providerID}, or 'provider/model'."""
    if isinstance(m, str):
        if "/" in m:
            p, _, i = m.partition("/")
            return {"provider": p, "model": i, "variant": None}
        return {"provider": None, "model": m, "variant": None}
    if isinstance(m, dict):
        return {
            "provider": m.get("providerID") or m.get("provider"),
            "model": m.get("id") or m.get("modelID") or m.get("model"),
            "variant": m.get("variant"),
        }
    return {"provider": None, "model": None, "variant": None}


def error_summary(err: Any) -> str | None:
    if err is None:
        return None
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        msg = err.get("message") or data.get("message")
        name = err.get("name") or err.get("type")
        code = data.get("statusCode")
        bits = [str(x) for x in (name, f"HTTP {code}" if code else None, msg) if x]
        return ": ".join(bits) if bits else json.dumps(err)[:300]
    return str(err)


def _text_of_content(content: Any) -> str:
    """v2 tool results are lists of {type:text,text}; tolerate strings and unknown shapes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    out.append(c["text"])
                else:
                    out.append(f"[{c.get('type', 'content')}{': ' + str(c.get('mime')) if c.get('mime') else ''}]")
            else:
                out.append(str(c))
        return "\n".join(out)
    return json.dumps(content, ensure_ascii=False)


CHILD_ID = re.compile(r"""<task[^>]*\bid=["'](ses_[A-Za-z0-9]+)["']""")


def make_tool(name, call_id, status, inp, output, error, metadata, started, ended, extra_raw=None) -> dict:
    command = None
    if isinstance(inp, dict):
        for k in COMMAND_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                command = v
                break
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                command = " ".join(v)
                break
    child = None
    if isinstance(metadata, dict):
        child = metadata.get("sessionId") or metadata.get("sessionID") or metadata.get("session_id")
    if not child and isinstance(output, str):
        m = CHILD_ID.search(output[:2000])
        child = m.group(1) if m else None
    duration = (ended - started) if isinstance(started, int) and isinstance(ended, int) and ended >= started else None
    return {
        "name": name or "unknown",
        "call_id": call_id,
        "status": status or "unknown",
        "input": safe_raw(inp),
        "command": command,
        "description": inp.get("description") if isinstance(inp, dict) and isinstance(inp.get("description"), str) else None,
        "output": output or "",
        "error": error_summary(error) if error is not None else None,
        "metadata": safe_raw(metadata) if metadata else None,
        "started": started,
        "ended": ended,
        "duration_ms": duration,
        "child_session_id": child if isinstance(child, str) else None,
    }


def _entry(id_, seq, kind, time_, parts, raw, **kw) -> dict:
    e = {
        "id": id_, "seq": seq, "kind": kind, "label": LABELS.get(kind, kind.upper()), "time": time_,
        "completed": None, "agent": None, "provider": None, "model": None, "variant": None,
        "tokens": None, "cost": None, "finish": None, "duration_ms": None, "error": None,
        "parts": parts, "raw": safe_raw(raw),
    }
    e.update(kw)
    return e


def unknown_part(raw: Any, type_name: str | None = None) -> dict:
    return {"type": "unknown", "original_type": type_name, "raw": safe_raw(raw)}


# ----------------------------------------------------------------------------------------
# v2 (session_message) records
# ----------------------------------------------------------------------------------------

def _v2_part(p: Any) -> dict:
    if not isinstance(p, dict):
        return unknown_part(p)
    t = p.get("type")
    if t in ("text", "reasoning"):
        return {"type": t, "text": p.get("text") if isinstance(p.get("text"), str) else _text_of_content(p.get("text")),
                "time": (p.get("time") or {}).get("created") if isinstance(p.get("time"), dict) else None}
    if t == "tool":
        st = p.get("state") if isinstance(p.get("state"), dict) else {}
        tm = p.get("time") if isinstance(p.get("time"), dict) else {}
        started = to_ms(tm.get("ran") or tm.get("created"))
        ended = to_ms(tm.get("completed"))
        output = _text_of_content(st.get("content")) if "content" in st else (
            st.get("output") if isinstance(st.get("output"), str) else "")
        tool = make_tool(p.get("name"), p.get("id"), st.get("status"), st.get("input"), output,
                         st.get("error"), st.get("metadata"), started, ended)
        tool["executed"] = p.get("executed")
        return {"type": "tool", "tool": tool, "time": to_ms(tm.get("created"))}
    return unknown_part(p, t if isinstance(t, str) else None)


def parse_v2_message(mtype: str, data: Any, id_: str, seq: int | None, time_created: Any) -> dict:
    d = data if isinstance(data, dict) else {}
    tm = d.get("time") if isinstance(d.get("time"), dict) else {}
    t0 = to_ms(tm.get("created")) or to_ms(time_created)
    if mtype == "user":
        parts = []
        if d.get("text"):
            parts.append({"type": "text", "text": d["text"] if isinstance(d["text"], str) else str(d["text"])})
        for i, f in enumerate(d.get("files") or []):
            if isinstance(f, dict):
                raw_len = len(f["data"]) if isinstance(f.get("data"), str) else None
                parts.append({"type": "file", "file": {
                    "index": i, "mime": f.get("mime"), "name": f.get("name"), "uri": f.get("uri") if isinstance(f.get("uri"), str) else None,
                    "encoded_chars": raw_len}})
        e = _entry(id_, seq, "user", t0, parts, d)
        m = norm_model(d.get("model"))
        e.update(provider=m["provider"], model=m["model"], variant=m["variant"], agent=d.get("agent"))
        return e
    if mtype == "assistant":
        parts = [_v2_part(p) for p in (d.get("content") or []) if p is not None]
        m = norm_model(d.get("model"))
        err = d.get("error")
        if err is not None:
            parts.append({"type": "error", "error": error_summary(err), "raw": safe_raw(err)})
        done = to_ms(tm.get("completed"))
        e = _entry(id_, seq, "assistant", t0, parts, d, completed=done, agent=d.get("agent"),
                   provider=m["provider"], model=m["model"], variant=m["variant"],
                   tokens=norm_tokens(d.get("tokens")), cost=_num(d.get("cost")), finish=d.get("finish") or d.get("rawFinish"),
                   duration_ms=(done - t0) if done and t0 and done >= t0 else None, error=error_summary(err))
        return e
    if mtype in ("system", "synthetic"):
        text = d.get("text") if isinstance(d.get("text"), str) else ""
        parts = [{"type": "text", "text": text, "synthetic": True}]
        e = _entry(id_, seq, mtype, t0, parts, d)
        e["description"] = d.get("description")
        e["source"] = (d.get("metadata") or {}).get("source") if isinstance(d.get("metadata"), dict) else None
        return e
    if mtype == "compaction":
        m = norm_model(d.get("model"))
        parts = [{"type": "text", "text": d.get("summary") if isinstance(d.get("summary"), str) else "", "synthetic": True}]
        e = _entry(id_, seq, "compaction", t0, parts, d, provider=m["provider"], model=m["model"], variant=m["variant"],
                   tokens=norm_tokens(d.get("tokens")), cost=_num(d.get("cost")))
        e["description"] = f"Context compaction ({d.get('reason') or 'unknown reason'}, {d.get('status') or 'unknown status'})"
        return e
    if mtype in ("idle", "agent-switched", "model-switched", "location-switched"):
        return _event_entry(mtype, d, id_, seq, t0)
    # unknown type: preserve everything
    e = _entry(id_, seq, "unknown", t0, [unknown_part(d, mtype)], d)
    e["original_type"] = mtype
    return e


def _event_entry(mtype, d, id_, seq, t0) -> dict:
    if mtype == "idle":
        text = f"Session idle (outcome: {d.get('outcome', 'unknown')})"
    elif mtype == "agent-switched":
        text = f"Agent switched: {d.get('previous')} → {d.get('agent')}"
    elif mtype == "model-switched":
        a, b = norm_model(d.get("previous")), norm_model(d.get("model"))
        text = f"Model switched: {a['provider']}/{a['model']} → {b['provider']}/{b['model']}"
    else:
        loc = (d.get("location") or {}).get("directory") if isinstance(d.get("location"), dict) else None
        prev = ((d.get("previous") or {}).get("location") or {}).get("directory") if isinstance(d.get("previous"), dict) else None
        text = f"Working directory changed: {prev} → {loc}"
    e = _entry(id_, seq, "event", t0, [{"type": "event", "text": text, "event": mtype}], d)
    e["label"] = "EVENT"
    e["event"] = mtype
    if mtype == "idle" and d.get("outcome") in ("failed", "error"):
        e["error"] = "session idle outcome: " + str(d.get("outcome"))
    return e


# ----------------------------------------------------------------------------------------
# legacy (message + part) records, also the shape of `opencode export`
# ----------------------------------------------------------------------------------------

def _legacy_part(p: Any) -> dict:
    if not isinstance(p, dict):
        return unknown_part(p)
    t = p.get("type")
    tm = p.get("time") if isinstance(p.get("time"), dict) else {}
    if t in ("text", "reasoning"):
        return {"type": t, "text": p.get("text") if isinstance(p.get("text"), str) else "",
                "synthetic": bool(p.get("synthetic") or p.get("ignored")), "time": to_ms(tm.get("start"))}
    if t == "tool":
        st = p.get("state") if isinstance(p.get("state"), dict) else {}
        stm = st.get("time") if isinstance(st.get("time"), dict) else {}
        out = st.get("output")
        tool = make_tool(p.get("tool"), p.get("callID"), st.get("status"), st.get("input"),
                         out if isinstance(out, str) else _text_of_content(out), st.get("error"),
                         st.get("metadata"), to_ms(stm.get("start")), to_ms(stm.get("end")))
        tool["title"] = st.get("title")
        return {"type": "tool", "tool": tool, "time": to_ms(stm.get("start"))}
    if t == "file":
        url = p.get("url") if isinstance(p.get("url"), str) else ""
        return {"type": "file", "file": {"index": None, "mime": p.get("mime"), "name": p.get("filename"),
                                         "uri": None if url.startswith("data:") else url,
                                         "encoded_chars": len(url) if url.startswith("data:") else None}}
    if t == "step-start":
        return {"type": "step", "step": "start"}
    if t == "step-finish":
        return {"type": "step", "step": "finish", "reason": p.get("reason"), "tokens": norm_tokens(p.get("tokens")),
                "cost": _num(p.get("cost"))}
    if t == "patch":
        return {"type": "patch", "hash": p.get("hash"), "files": p.get("files") if isinstance(p.get("files"), list) else []}
    if t == "subtask":
        return {"type": "text", "synthetic": True,
                "text": f"[subtask → agent {p.get('agent')}] {p.get('description') or ''}\n{p.get('prompt') or ''}".strip()}
    return unknown_part(p, t if isinstance(t, str) else None)


def parse_legacy_message(info: Any, parts: list[Any], id_: str | None = None, time_created: Any = None) -> dict:
    d = info if isinstance(info, dict) else {}
    role = d.get("role") if d.get("role") in ("user", "assistant", "system") else None
    tm = d.get("time") if isinstance(d.get("time"), dict) else {}
    t0 = to_ms(tm.get("created")) or to_ms(time_created)
    norm_parts = [_legacy_part(p) for p in parts]
    err = d.get("error")
    if err is not None:
        norm_parts.append({"type": "error", "error": error_summary(err), "raw": safe_raw(err)})
    raw = {"info": d, "parts": parts}
    if role is None:
        e = _entry(id_ or d.get("id"), None, "unknown", t0, [unknown_part(raw, "message")], raw)
        e["original_type"] = d.get("role")
        return e
    if role == "assistant":
        mm = {"provider": d.get("providerID"), "model": d.get("modelID"), "variant": d.get("variant")}
    else:
        mm = norm_model(d.get("model"))
        mm["variant"] = mm["variant"] or d.get("variant")
    done = to_ms(tm.get("completed"))
    e = _entry(id_ or d.get("id"), None, role, t0, norm_parts, raw, completed=done, agent=d.get("agent") or d.get("mode"),
               provider=mm["provider"], model=mm["model"], variant=mm["variant"],
               tokens=norm_tokens(d.get("tokens")), cost=_num(d.get("cost")), finish=d.get("finish"),
               duration_ms=(done - t0) if done and t0 and done >= t0 else None, error=error_summary(err))
    return e


def transcript_stats(entries: list[dict]) -> dict:
    """Aggregates used by the index: errors, tools, models. Derived only from stored data."""
    errors = tools = 0
    tool_names: dict[str, int] = {}
    models: set[str] = set()
    for e in entries:
        if e.get("error") and e["kind"] != "event":
            errors += 1
        elif e.get("kind") == "event" and e.get("error"):
            errors += 1
        if e.get("model"):
            models.add(f"{e.get('provider') or '?'}/{e['model']}")
        for p in e["parts"]:
            if p["type"] == "tool":
                tools += 1
                n = p["tool"]["name"]
                tool_names[n] = tool_names.get(n, 0) + 1
                if p["tool"]["status"] == "error" or p["tool"]["error"]:
                    errors += 1
    return {"error_count": errors, "tool_count": tools, "tools": tool_names, "models": sorted(models)}
