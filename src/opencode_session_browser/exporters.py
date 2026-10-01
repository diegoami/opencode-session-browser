"""Read-only exports: raw-ish JSON and readable Markdown."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def fmt_ts(ms) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return str(ms)


def clip_text(s: str, limit: int) -> tuple[str, int | None]:
    if limit and isinstance(s, str) and len(s) > limit:
        return s[:limit], len(s)
    return s, None


def _clip_json(v, limit: int):
    if isinstance(v, str):
        return v[:limit] + f"… [{len(v) - limit} more chars]" if len(v) > limit else v
    if isinstance(v, dict):
        return {k: _clip_json(x, limit) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip_json(x, limit) for x in v]
    return v


def light_entry(e: dict, limit: int) -> dict:
    """Entry for the viewer: no raw payload, long strings clipped (full text fetched on demand)."""
    out = {k: v for k, v in e.items() if k not in ("raw", "parts")}
    parts = []
    for p in e["parts"]:
        p = dict(p)
        if p["type"] in ("text", "reasoning") and isinstance(p.get("text"), str):
            p["text"], full = clip_text(p["text"], limit)
            if full:
                p["truncated"] = full
        elif p["type"] == "tool":
            t = dict(p["tool"])
            t["output"], full = clip_text(t.get("output") or "", limit)
            if full:
                t["output_truncated"] = full
            t["input"] = _clip_json(t.get("input"), limit) if limit else t.get("input")
            if t.get("command") and len(t["command"]) > limit:
                t["command"] = t["command"][:limit]
            p["tool"] = t
        elif p["type"] == "unknown":
            p["raw"] = _clip_json(p.get("raw"), min(limit, 4000))
        parts.append(p)
    out["parts"] = parts
    return out


def export_json(detail: dict, entries: list[dict]) -> str:
    doc = {
        "exported_by": "opencode-session-browser",
        "note": "Read-only export. Keys that look like secrets are redacted and large inline binary data is elided.",
        "session": {k: v for k, v in detail.items() if k not in ("tree",)},
        "messages": entries,
    }
    return json.dumps(doc, ensure_ascii=False, indent=2, default=str)


def _fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text}\n{ticks}"


def export_markdown(detail: dict, entries: list[dict]) -> str:
    L = [f"# {detail.get('title') or detail['id']}", ""]
    meta = [("Session ID", detail["id"]), ("Source", detail.get("source_label")), ("Project", detail.get("project_label")),
            ("Directory", detail.get("directory")), ("Created", fmt_ts(detail.get("created"))), ("Updated", fmt_ts(detail.get("updated"))),
            ("Model", "/".join(x for x in (detail.get("provider"), detail.get("model")) if x) or None),
            ("Agent", detail.get("agent")), ("Parent", detail.get("parent_id")), ("OpenCode version", detail.get("version")),
            ("Cost", detail.get("cost"))]
    for k, v in meta:
        if v not in (None, "", "?"):
            L.append(f"- **{k}:** {v}")
    L.append("")
    for e in entries:
        head = f"## {e['label']}"
        bits = [fmt_ts(e.get("time"))]
        if e.get("model"):
            bits.append(f"{e.get('provider') or ''}/{e['model']}".strip("/"))
        if e.get("agent"):
            bits.append(f"agent {e['agent']}")
        L += [f"{head} — {' · '.join(b for b in bits if b and b != '?')}", ""]
        for p in e["parts"]:
            t = p["type"]
            if t == "text":
                L += [p.get("text", ""), ""]
            elif t == "reasoning":
                L += ["<details><summary>reasoning</summary>", "", p.get("text", ""), "", "</details>", ""]
            elif t == "tool":
                tool = p["tool"]
                L.append(f"**TOOL CALL `{tool['name']}`** — {tool['status']}" + (f" — {tool['duration_ms']} ms" if tool.get("duration_ms") is not None else ""))
                L.append("")
                if tool.get("command"):
                    L += [_fence(tool["command"], "bash"), ""]
                elif tool.get("input"):
                    L += [_fence(json.dumps(tool["input"], ensure_ascii=False, indent=2), "json"), ""]
                if tool.get("output"):
                    L += ["**TOOL RESULT**", "", _fence(tool["output"]), ""]
                if tool.get("error"):
                    L += [f"**ERROR:** {tool['error']}", ""]
            elif t == "error":
                L += [f"**ERROR:** {p.get('error')}", ""]
            elif t == "file":
                f = p["file"]
                L += [f"*[attachment: {f.get('name') or ''} {f.get('mime') or ''}]*", ""]
            elif t == "event":
                L += [f"*{p.get('text')}*", ""]
            elif t == "patch":
                L += [f"*[patch {p.get('hash')}: {', '.join(map(str, p.get('files') or []))}]*", ""]
            elif t == "unknown":
                L += [f"*[unknown part type: {p.get('original_type')}]*", "", _fence(json.dumps(p.get("raw"), ensure_ascii=False)[:2000], "json"), ""]
        usage = []
        if e.get("tokens"):
            usage.append("tokens " + ", ".join(f"{k}={v}" for k, v in e["tokens"].items() if v is not None))
        if e.get("cost"):
            usage.append(f"cost {e['cost']}")
        if e.get("finish"):
            usage.append(f"finish {e['finish']}")
        if usage:
            L += [f"<sub>{' · '.join(usage)}</sub>", ""]
    return "\n".join(L)
