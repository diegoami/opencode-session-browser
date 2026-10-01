"use strict";
// OpenCode Session Browser UI. Vanilla JS, no external resources. Read-only: it only issues GETs
// (plus POST /api/refresh, which re-reads sources).
const $ = (s, r = document) => r.querySelector(s);
const app = $("#app");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts) => { const r = await fetch(path, opts); if (!r.ok) throw new Error((await r.text()).slice(0, 300)); return r.json(); };
const fmtT = ms => ms ? new Date(ms).toLocaleString() : "—";
const fmtS = ms => ms ? new Date(ms).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "—";
const ago = ms => { if (!ms) return ""; const s = (Date.now() - ms) / 1000; if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + "m ago"; if (s < 86400) return Math.floor(s / 3600) + "h ago"; if (s < 86400 * 60) return Math.floor(s / 86400) + "d ago"; return ""; };
const fmtN = n => n == null ? "—" : n.toLocaleString();
const fmtDur = ms => ms == null ? "" : ms < 1000 ? ms + " ms" : ms < 60000 ? (ms / 1000).toFixed(1) + " s" : Math.floor(ms / 60000) + "m " + Math.round((ms % 60000) / 1000) + "s";
const fmtCost = c => c == null ? "—" : c === 0 ? "$0" : "$" + (c < 0.01 ? c.toFixed(5) : c.toFixed(3));
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 1400); }
async function copy(text) {
  try { await navigator.clipboard.writeText(text); } catch { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
  toast("Copied");
}
document.addEventListener("click", e => { const b = e.target.closest("[data-copy]"); if (b) { e.preventDefault(); e.stopPropagation(); copy(b.dataset.copy); } });

// ---------- tiny markdown renderer (escapes everything first) ----------
function md(src) {
  const blocks = [];
  let text = String(src ?? "").replace(/\r\n/g, "\n").replace(/```([^\n`]*)\n([\s\S]*?)(```|$)/g, (_, lang, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = s => esc(s)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const lines = text.split("\n"); const out = []; let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    let m;
    if (/^\u0000\d+\u0000$/.test(l.trim())) { out.push(blocks[+l.trim().slice(1, -1)]); i++; continue; }
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { out.push(`<h${Math.min(m[1].length + 1, 5)}>${inline(m[2])}</h${Math.min(m[1].length + 1, 5)}>`); i++; continue; }
    if (/^\s*([-*_]){3,}\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*>/.test(l)) { const q = []; while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, "")); out.push(`<blockquote>${inline(q.join("\n"))}</blockquote>`); continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || "")) {
      const rows = []; while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
      out.push("<table><thead><tr>" + cells(rows[0]).map(c => `<th>${c}</th>`).join("") + "</tr></thead><tbody>" +
        rows.slice(2).map(r => "<tr>" + cells(r).map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>"); continue;
    }
    if ((m = l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      const ord = /\d/.test(m[2]); const items = [];
      while (i < lines.length && (m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) { items.push(inline(m[3])); i++; }
      out.push(`<${ord ? "ol" : "ul"}>` + items.map(x => `<li>${x}</li>`).join("") + `</${ord ? "ol" : "ul"}>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*>|\s*([-*+]|\d+[.)])\s|\u0000)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) { para.push(lines[i++]); }
    out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }
  return `<div class="md">${out.join("")}</div>`;
}

// ---------- state & routing ----------
const state = { filters: { sort: "updated", order: "desc", kind: "all", archived: "include", limit: 100 }, q: "", sel: { source: new Set() }, rows: [], total: 0, facets: {}, version: -1, sources: null,
  view: null, session: null, fp: null, show: { reasoning: false, tools: true, events: true, steps: false } };
const parseHash = () => { const h = location.hash.slice(1) || "/"; const [p, qs] = h.split("?"); return { parts: p.split("/").filter(Boolean).map(decodeURIComponent), qs: new URLSearchParams(qs || "") }; };
window.addEventListener("hashchange", route);
$("#searchform").addEventListener("submit", e => { e.preventDefault(); state.q = $("#q").value.trim(); if (parseHash().parts[0]) location.hash = "#/"; else loadList(); });
$("#q").addEventListener("input", debounce(() => { const v = $("#q").value.trim(); if (v !== state.q && (location.hash.slice(1) || "/") === "/") { state.q = v; loadList(); } }, 350));
$("#refresh").addEventListener("click", async () => { toast("Refreshing…"); await api("/api/refresh", { method: "POST" }); await route(true); });
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function route(force) {
  const { parts, qs } = parseHash();
  for (const a of document.querySelectorAll("nav a")) a.classList.remove("on");
  if (parts[0] === "s" && parts[1]) { state.view = "session"; await showSession(parts[1], qs.get("m")); }
  else if (parts[0] === "sources") { state.view = "sources"; $("#nav-sources").classList.add("on"); await showSources(); }
  else { state.view = "list"; $("#nav-sessions").classList.add("on"); if (!$("#rows") || force === true) renderListShell(); await loadList(); }
}

// ---------- session list ----------
function params() {
  const f = state.filters, p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.sel.source.size) p.set("source", [...state.sel.source].join(","));
  for (const k of ["project", "model", "outcome", "status", "kind", "archived", "since", "until", "field", "in", "sort", "order", "limit", "errors", "v2only"]) if (f[k]) p.set(k, f[k]);
  return p;
}
function renderListShell() {
  app.innerHTML = `<div class="layout"><aside class="filters" id="filters"></aside><section><div id="summary" class="muted"></div><table><thead><tr id="thead"></tr></thead><tbody id="rows"></tbody></table><div id="more"></div></section></div>`;
}
const COLS = [["updated", "Updated"], ["created", "Created"], ["source", "Source"], ["project", "Project"], ["title", "Title / ID"], [null, "Model"], ["messages", "Msgs"], [null, "Children"], [null, "Status"], ["errors", "Err"]];
async function loadList(more) {
  if (state.view !== "list") return;
  if (!state.sources) { try { state.sources = (await api("/api/sources")).sources; } catch { /* retry on next load */ } }
  const p = params(); if (more) p.set("offset", state.rows.length);
  let d; try { d = await api("/api/sessions?" + p); } catch (e) { app.innerHTML = `<div class="errbox">${esc(e.message)}</div>`; return; }
  state.rows = more ? state.rows.concat(d.rows) : d.rows; state.total = d.total; state.facets = d.facets; state.version = d.version; state.indexing = d.indexing;
  renderList(); updateIndexState(d.indexing);
}
function updateIndexState(ix) { $("#indexstate").textContent = ix && ix.running ? `indexing ${ix.done}/${ix.total}…` : ""; }
function renderList() {
  if (!$("#rows")) renderListShell();
  const f = state.filters, fc = state.facets;
  const opt = (obj, cur) => Object.entries(obj || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `<option value="${esc(k)}" ${k === cur ? "selected" : ""}>${esc(k)} (${n})</option>`).join("");
  const srcs = state.sources || [];
  $("#filters").innerHTML = `
   <h4>Source</h4>${(state.sources || []).map(sv => `<label><input type="checkbox" data-src="${esc(sv.id)}" ${state.sel.source.has(sv.id) ? "checked" : ""}>${esc(sv.label)}<span class="cnt">${(fc.source || {})[sv.id] || 0}</span></label>`).join("") || '<span class="muted">none</span>'}
   <h4>Project</h4><select id="f-project"><option value="">All</option>${opt(fc.project, f.project)}</select>
   <h4>Provider / model</h4><select id="f-model"><option value="">All</option>${opt(fc.model, f.model)}</select>
   <h4>Last outcome</h4><select id="f-outcome"><option value="">Any</option>${opt(fc.outcome, f.outcome)}</select>
   <h4>Live status</h4><select id="f-status"><option value="">Any</option>${["busy", "retry", "idle", "unknown"].map(s => `<option ${f.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
   <h4>Kind</h4><select id="f-kind"><option value="all">All sessions</option><option value="root" ${f.kind === "root" ? "selected" : ""}>Top-level only</option><option value="child" ${f.kind === "child" ? "selected" : ""}>Child / subagent only</option></select>
   <h4>Archived</h4><select id="f-archived">${["include", "exclude", "only"].map(s => `<option ${f.archivedSel === s || f.archived === s ? "selected" : ""}>${s}</option>`).join("")}</select>
   <h4>Date (${f.field === "created" ? "created" : "updated"})</h4><input type="date" id="f-since" value="${f.sinceD || ""}"> <input type="date" id="f-until" value="${f.untilD || ""}">
   <label><input type="radio" name="df" value="updated" ${f.field !== "created" ? "checked" : ""}>updated</label><label><input type="radio" name="df" value="created" ${f.field === "created" ? "checked" : ""}>created</label>
   <h4>Flags</h4><label><input type="checkbox" id="f-errors" ${f.errors ? "checked" : ""}>With errors</label><label><input type="checkbox" id="f-v2only" ${f.v2only ? "checked" : ""}>Not in OpenCode CLI/Web tables</label>
   ${state.q ? `<h4>Search in</h4>${["meta", "user", "assistant", "tool", "output", "error", "system"].map(k => `<label><input type="checkbox" data-in="${k}" ${(f.in || "").split(",").includes(k) ? "checked" : ""}>${k === "meta" ? "title/id/path" : k === "tool" ? "tool names+inputs" : k === "output" ? "tool results" : k}</label>`).join("")}` : ""}
   <p><button id="f-reset">Reset filters</button></p>`;
  $("#thead").innerHTML = COLS.map(([k, l]) => `<th data-sort="${k || ""}">${l}${f.sort === k ? (f.order === "asc" ? " ▲" : " ▼") : ""}</th>`).join("");
  $("#summary").textContent = `${fmtN(state.total)} session${state.total === 1 ? "" : "s"}` + (state.q ? ` matching “${state.q}”` : "") + `, showing ${state.rows.length}`;
  $("#rows").innerHTML = state.rows.map(rowHtml).join("") || `<tr><td colspan="10" class="empty">No sessions match.</td></tr>`;
  $("#more").innerHTML = state.rows.length < state.total ? `<p><button id="loadmore">Load more (${state.total - state.rows.length} left)</button></p>` : "";
  wireList();
}
function srcLabel(id) { const r = state.rows.find(r => r.source_id === id); if (r) return r.source_label; const s = (state.sources || []).find(s => s.id === id); return s ? s.label : id; }
function statusBadge(r) {
  let h = `<span class="badge ${r.status === "busy" ? "b-warn" : ""}" title="${esc(r.status_basis)}">${esc(r.status)}</span>`;
  if (r.outcome) h += `<span class="badge ${r.outcome === "failed" ? "b-err" : r.outcome === "succeeded" ? "b-ok" : "b-warn"}" title="last stored idle outcome">${esc(r.outcome)}</span>`;
  if (r.archived_at) h += `<span class="badge">archived</span>`;
  return h;
}
function rowHtml(r) {
  const model = r.model ? `${r.provider ? r.provider + "/" : ""}${r.model}${r.variant ? " · " + r.variant : ""}` : (r.models || [])[0] || "";
  const more = !r.model && (r.models || []).length > 1 ? ` +${r.models.length - 1}` : "";
  const snips = (r.matches || []).map(m => `<div class="snip"><span class="badge">${esc(m.kind)}${m.tool ? " " + esc(m.tool) : ""}</span>${hl(m)}</div>`).join("");
  return `<tr class="row" data-key="${esc(r.key)}"><td title="${esc(fmtT(r.updated))}" style="white-space:nowrap">${esc(fmtS(r.updated))}<div class="sub">${esc(ago(r.updated))}</div></td>
   <td class="sub" style="white-space:nowrap">${esc(fmtS(r.created))}</td><td><span class="badge b-src">${esc(r.source_label)}</span></td>
   <td title="${esc(r.directory)}" style="max-width:240px;overflow-wrap:anywhere">${esc(r.project_label)}<div class="sub mono">${esc(r.directory || "")}</div></td>
   <td><div class="title">${r.parent_id ? "↳ " : ""}${esc(r.title || "(untitled)")}</div><div class="sub mono">${esc(r.id)} <a href="#" data-copy="${esc(r.id)}" title="copy session id">copy</a>${r.in_legacy_table === false ? ' <span class="badge b-warn" title="Present only in v2 tables: not shown by opencode export / session list">v2-only</span>' : ""}</div>${snips}</td>
   <td class="sub">${esc(model)}${more}</td><td class="num">${fmtN(r.message_count)}</td><td class="num">${r.child_count || ""}</td><td>${statusBadge(r)}</td>
   <td class="num">${r.error_count == null ? (r.outcome === "failed" ? '<span class="badge b-err">!</span>' : '<span class="muted" title="not indexed yet">?</span>') : r.error_count ? `<span class="badge b-err">${r.error_count}</span>` : ""}</td></tr>`;
}
function hl(m) { const s = m.snippet, a = m.start, b = a + m.length; return esc(s.slice(0, a)) + "<mark>" + esc(s.slice(a, b)) + "</mark>" + esc(s.slice(b)); }
function wireList() {
  const f = state.filters, on = (id, fn) => { const e = $(id); if (e) e.addEventListener("change", fn); };
  for (const c of document.querySelectorAll("[data-src]")) c.addEventListener("change", () => { c.checked ? state.sel.source.add(c.dataset.src) : state.sel.source.delete(c.dataset.src); loadList(); });
  for (const c of document.querySelectorAll("[data-in]")) c.addEventListener("change", () => { f.in = [...document.querySelectorAll("[data-in]:checked")].map(x => x.dataset.in).join(","); loadList(); });
  on("#f-project", e => { f.project = e.target.value; loadList(); }); on("#f-model", e => { f.model = e.target.value; loadList(); });
  on("#f-outcome", e => { f.outcome = e.target.value; loadList(); }); on("#f-status", e => { f.status = e.target.value; loadList(); });
  on("#f-kind", e => { f.kind = e.target.value; loadList(); }); on("#f-archived", e => { f.archived = e.target.value; loadList(); });
  on("#f-errors", e => { f.errors = e.target.checked ? "1" : ""; loadList(); }); on("#f-v2only", e => { f.v2only = e.target.checked ? "1" : ""; loadList(); });
  const dt = (id, k, dk, end) => on(id, e => { const v = e.target.value; f[dk] = v; f[k] = v ? String(new Date(v + (end ? "T23:59:59.999" : "T00:00:00")).getTime()) : ""; loadList(); });
  dt("#f-since", "since", "sinceD", false); dt("#f-until", "until", "untilD", true);
  for (const r of document.querySelectorAll("[name=df]")) r.addEventListener("change", () => { f.field = r.value; loadList(); });
  $("#f-reset").addEventListener("click", () => { state.filters = { sort: "updated", order: "desc", kind: "all", archived: "include", limit: 100 }; state.sel.source.clear(); state.q = ""; $("#q").value = ""; loadList(); });
  for (const th of document.querySelectorAll("th[data-sort]")) th.addEventListener("click", () => { const k = th.dataset.sort; if (!k) return; f.order = f.sort === k && f.order === "desc" ? "asc" : "desc"; f.sort = k; loadList(); });
  for (const tr of document.querySelectorAll("tr.row")) tr.addEventListener("click", e => { if (e.target.closest("a")) return; const m = (state.rows.find(r => r.key === tr.dataset.key).matches || [])[0]; location.hash = "#/s/" + encodeURIComponent(tr.dataset.key) + (m ? "?m=" + encodeURIComponent(m.message_id) : ""); });
  const more = $("#loadmore"); if (more) more.addEventListener("click", () => loadList(true));
}

// ---------- session viewer ----------
async function showSession(key, focusMsg) {
  app.innerHTML = '<div class="empty">Loading session…</div>';
  let d; try { d = await api("/api/sessions/" + encodeURIComponent(key)); } catch (e) { app.innerHTML = `<div class="errbox">${esc(e.message)}</div><p><a href="#/">← back</a></p>`; return; }
  state.session = d; state.msgs = new Map();
  const tok = d.tokens || {};
  const lin = d.lineage || [];
  const crumbs = lin.length ? `<div class="crumbs">${lin.map(l => l.key ? `<a href="#/s/${encodeURIComponent(l.key)}">${esc(l.title || l.id)}</a>` : `<span class="muted">${esc(l.title)} <span class="mono">${esc(l.id)}</span></span>`).join(" › ")} › <strong>${esc(d.title || d.id)}</strong> ${d.root_key && d.root_key !== d.key ? `<a href="#/s/${encodeURIComponent(d.root_key)}" class="badge">↑ root</a>` : ""}${lin.length ? `<a class="badge" href="#/s/${encodeURIComponent(lin[lin.length - 1].key || "")}">↑ parent</a>` : ""}</div>` : "";
  app.innerHTML = `<p><a href="#/">← sessions</a></p>${crumbs}
  <div class="shead"><h2>${esc(d.title || "(untitled)")}</h2>
   <dl class="kv">
    <dt>Session ID</dt><dd class="mono">${esc(d.id)} <button class="small" data-copy="${esc(d.id)}">copy</button></dd>
    <dt>Source</dt><dd>${esc(d.source_label)} <span class="muted">(${esc(d.source_env)}, storage: ${esc(d.storage)})</span>${d.in_legacy_table === false ? ' <span class="badge b-warn">v2-only: absent from OpenCode CLI/Web tables</span>' : ""}</dd>
    <dt>Project</dt><dd>${esc(d.project_label)} <span class="muted mono">${esc(d.project_worktree || "")}</span></dd>
    <dt>Directory</dt><dd class="mono">${esc(d.directory || "—")} ${d.directory ? `<button class="small" data-copy="${esc(d.directory)}">copy</button>` : ""} ${d.directory_exists === false ? '<span class="badge b-warn">directory no longer exists</span>' : ""}</dd>
    <dt>Created / updated</dt><dd>${esc(fmtT(d.created))} / ${esc(fmtT(d.updated))}${d.archived_at ? ` · archived ${esc(fmtT(d.archived_at))}` : ""}</dd>
    <dt>Model</dt><dd>${esc([d.provider, d.model].filter(Boolean).join("/") || "—")}${d.variant ? " · " + esc(d.variant) : ""}${(d.models || []).length ? ` <span class="muted">used: ${esc(d.models.join(", "))}</span>` : ""}${d.agent ? ` · agent ${esc(d.agent)}` : ""}</dd>
    <dt>Usage</dt><dd>${Object.keys(tok).length ? Object.entries(tok).map(([k, v]) => `${k.replace("_", " ")} ${fmtN(v)}`).join(" · ") : "—"} · cost ${fmtCost(d.cost)}</dd>
    <dt>Status</dt><dd>${statusBadge(d)} <span class="muted">${esc(d.status_basis)}</span></dd>
    <dt>OpenCode</dt><dd>version ${esc(d.version || "?")}${d.parent_id ? ` · parent <span class="mono">${esc(d.parent_id)}</span>` : ""}${d.fork_of ? ` · forked from <span class="mono">${esc(d.fork_of)}</span>` : ""}</dd>
   </dl>
   <div class="toolbar">
    <a href="/api/sessions/${encodeURIComponent(d.key)}/export.json"><button>Export JSON</button></a>
    <a href="/api/sessions/${encodeURIComponent(d.key)}/export.md"><button>Export Markdown</button></a>
    ${(d.resume || []).map(r => `<button data-copy="${esc(r.command)}" title="${esc(r.command)}">Copy resume (${esc(r.label)})</button>`).join("")}
    <label><input type="checkbox" id="sh-reason" ${state.show.reasoning ? "checked" : ""}> reasoning</label>
    <label><input type="checkbox" id="sh-tools" ${state.show.tools ? "checked" : ""}> tools</label>
    <label><input type="checkbox" id="sh-events" ${state.show.events ? "checked" : ""}> events</label>
    <label><input type="checkbox" id="sh-steps" ${state.show.steps ? "checked" : ""}> steps</label>
    <button id="expand">Expand tools</button><button id="collapse">Collapse tools</button>
   </div>${d.resume_warning ? `<div class="sub">⚠ ${esc(d.resume_warning)} Resume commands are only copied, never executed.</div>` : `<div class="sub">Resume commands are only copied, never executed.</div>`}</div>
  <div class="cols"><div id="transcript"><div class="empty">Loading messages…</div></div>
  <aside class="tree"><h4>Session tree</h4>${treeHtml(d.tree, d.id)}</aside></div>`;
  for (const [id, k] of [["sh-reason", "reasoning"], ["sh-tools", "tools"], ["sh-events", "events"], ["sh-steps", "steps"]]) $("#" + id).addEventListener("change", e => { state.show[k] = e.target.checked; renderTranscript(true); });
  $("#expand").addEventListener("click", () => document.querySelectorAll("details.tool").forEach(x => x.open = true));
  $("#collapse").addEventListener("click", () => document.querySelectorAll("details.tool").forEach(x => x.open = false));
  await loadMessages(true, focusMsg);
}
function treeHtml(n, cur) {
  if (!n) return "";
  const kids = (n.children || []).length ? "<ul>" + n.children.map(c => treeHtml(c, cur)).join("") + "</ul>" : "";
  return `<ul class="rootul"><li class="${n.id === cur ? "cur" : ""}"><a href="#/s/${encodeURIComponent(n.key)}">${esc(n.title || n.id)}</a> <span class="sub">${n.message_count ?? ""}${n.error_count ? ` · <span style="color:var(--err)">${n.error_count} err</span>` : ""}</span>${kids}</li></ul>`.replace(/^<ul class="rootul">/, "<ul>");
}
async function loadMessages(first, focus) {
  const d = state.session; if (!d) return;
  const r = await api(`/api/sessions/${encodeURIComponent(d.key)}/messages`);
  state.fp = r.fingerprint; state.list = r.messages;
  renderTranscript(false, focus);
}
function renderTranscript(forceAll, focus) {
  const box = $("#transcript"); if (!box) return;
  const sy = window.scrollY; const nearBottom = window.innerHeight + sy >= document.body.scrollHeight - 120;
  const msgs = state.list;
  if (!msgs.length) { box.innerHTML = '<div class="empty">No stored messages for this session.</div>'; return; }
  if (forceAll || !box.dataset.n) { box.innerHTML = ""; state.rendered = new Map(); }
  for (const m of msgs) {
    const html = msgHtml(m); const prev = state.rendered.get(m.id);
    if (prev && prev.html === html) continue;
    const el = document.createElement("div"); el.innerHTML = html; const node = el.firstElementChild;
    if (prev) { const open = [...prev.node.querySelectorAll("details")].map(x => x.open); node.querySelectorAll("details").forEach((x, i) => { if (open[i]) x.open = true; }); prev.node.replaceWith(node); }
    else box.appendChild(node);
    state.rendered.set(m.id, { html, node });
  }
  box.dataset.n = msgs.length;
  wireTranscript(box);
  if (focus) { const t = box.querySelector(`[data-mid="${CSS.escape(focus)}"]`); if (t) { t.classList.add("hit"); t.scrollIntoView({ block: "center" }); } }
  else if (state.stick && nearBottom) window.scrollTo(0, document.body.scrollHeight);
  else window.scrollTo(0, sy);
}
function msgHtml(m) {
  const show = state.show; const cls = m.kind;
  if (m.kind === "event" && !show.events) return `<div data-mid="${esc(m.id)}" hidden></div>`;
  const hasErr = !!m.error || m.parts.some(p => p.type === "tool" && p.tool.error);
  const meta = [];
  if (m.model) meta.push(`${esc(m.provider || "")}${m.provider ? "/" : ""}${esc(m.model)}${m.variant ? " · " + esc(m.variant) : ""}`);
  if (m.agent) meta.push("agent " + esc(m.agent));
  if (m.duration_ms != null) meta.push(esc(fmtDur(m.duration_ms)));
  if (m.tokens) meta.push("tokens " + Object.entries(m.tokens).filter(([, v]) => v != null && v !== 0).map(([k, v]) => `${k.replace("_", " ")} ${fmtN(v)}`).join(" · "));
  if (m.cost) meta.push(fmtCost(m.cost));
  if (m.finish) meta.push("finish: " + esc(m.finish));
  const parts = m.parts.map(p => partHtml(m, p)).join("");
  const label = m.kind === "synthetic" ? "SYSTEM · synthetic" : m.kind === "compaction" ? "SYSTEM · compaction" : m.label;
  if (m.kind === "event") return `<div class="msg event" data-mid="${esc(m.id)}">${esc(fmtT(m.time))} — ${parts.replace(/<[^>]+>/g, "")}</div>`;
  return `<div class="msg ${cls} ${hasErr ? "haserr" : ""}" data-mid="${esc(m.id)}"><div class="mh"><span class="role ${m.kind === "assistant" ? "assistant" : m.kind === "user" ? "user" : "system"}">${esc(label)}</span><span>${esc(fmtT(m.time))}</span>${meta.map(x => `<span>· ${x}</span>`).join("")}<span class="sp"></span>${m.description ? `<span>${esc(m.description)}</span>` : ""}<button class="small raw" data-raw="${esc(m.id)}">raw</button></div>${parts}<div class="rawbox"></div></div>`;
}
function partHtml(m, p) {
  const show = state.show;
  switch (p.type) {
    case "text": return `<div class="part">${m.kind === "user" ? `<div class="md"><pre style="white-space:pre-wrap;max-height:none;background:none;padding:0;font-family:inherit;font-size:inherit">${esc(p.text)}</pre></div>` : md(p.text)}${truncNote(m, p.truncated)}</div>`;
    case "reasoning": return show.reasoning ? `<details class="reason" open><summary>reasoning</summary>${md(p.text)}${truncNote(m, p.truncated)}</details>` : "";
    case "tool": return show.tools ? toolHtml(m, p) : "";
    case "file": return `<div class="part"><span class="badge">📎 ${esc(p.file.name || "attachment")} ${esc(p.file.mime || "")}${p.file.encoded_chars ? " · " + fmtN(Math.round(p.file.encoded_chars * 0.75)) + " bytes" : ""}</span></div>`;
    case "error": return `<div class="errbox"><strong>ERROR</strong> ${esc(p.error)}</div>`;
    case "step": return show.steps ? `<div class="sub">step ${esc(p.step)}${p.reason ? " · " + esc(p.reason) : ""}${p.tokens ? " · tokens " + esc(JSON.stringify(p.tokens)) : ""}</div>` : "";
    case "patch": return `<div class="part sub">patch <span class="mono">${esc((p.hash || "").slice(0, 10))}</span>: ${esc((p.files || []).join(", "))}</div>`;
    case "event": return esc(p.text);
    default: return `<details class="tool"><summary><span class="tn">unknown part type</span> <span class="mono">${esc(p.original_type || "?")}</span></summary><div class="tbody"><pre>${esc(JSON.stringify(p.raw, null, 2))}</pre></div></details>`;
  }
}
function truncNote(m, full) { return full ? `<div class="sub">⚠ truncated (${fmtN(full)} chars stored) <button class="small full" data-full="${esc(m.id)}">load full message</button></div>` : ""; }
function toolHtml(m, p) {
  const t = p.tool, err = t.status === "error" || t.error; const inputKeys = t.input && typeof t.input === "object" ? Object.keys(t.input) : [];
  const brief = t.command ? t.command.split("\n")[0].slice(0, 120) : t.description || (inputKeys.length ? (t.input.filePath || t.input.path || t.input.pattern || t.input.query || t.input.url || "") : "");
  const child = t.child_session_id && state.session ? `<a href="#/s/${encodeURIComponent(state.session.source_id + "~" + t.child_session_id)}" class="badge">→ child session</a>` : "";
  return `<details class="tool ${err ? "err" : ""}"><summary><span class="role" style="color:var(--tool)">TOOL CALL</span><span class="tn">${esc(t.name)}</span><span class="badge ${err ? "b-err" : "b-ok"}">${esc(t.status)}</span><span class="sub mono">${esc(String(brief).slice(0, 140))}</span><span class="sub">${esc(fmtDur(t.duration_ms))}</span>${child}</summary><div class="tbody">
   ${t.started ? `<div class="sub">${esc(fmtT(t.started))}${t.ended ? " → " + esc(fmtT(t.ended)) : ""}${t.call_id ? ` · call ${esc(t.call_id)}` : ""}</div>` : ""}
   ${t.command ? `<div class="lbl">Command <button class="small" data-copy="${esc(t.command)}">copy</button></div><pre class="cmd">${esc(t.command)}</pre>` : ""}
   ${t.input != null && (!t.command || inputKeys.length > 1) ? `<div class="lbl">Input <button class="small" data-copy="${esc(JSON.stringify(t.input, null, 2))}">copy</button></div><pre>${esc(typeof t.input === "string" ? t.input : JSON.stringify(t.input, null, 2))}</pre>` : ""}
   ${t.output ? `<div class="lbl">Result <button class="small" data-copy="${esc(t.output)}">copy</button></div><pre>${esc(t.output)}</pre>${t.output_truncated ? `<div class="sub">⚠ truncated (${fmtN(t.output_truncated)} chars) <button class="small full" data-full="${esc(m.id)}">load full message</button></div>` : ""}` : ""}
   ${t.error ? `<div class="errbox"><strong>ERROR</strong> ${esc(t.error)}</div>` : ""}
   ${t.metadata ? `<details><summary class="sub">metadata</summary><pre>${esc(JSON.stringify(t.metadata, null, 2))}</pre></details>` : ""}</div></details>`;
}
function wireTranscript(box) {
  for (const b of box.querySelectorAll("button.raw:not([data-w])")) { b.dataset.w = 1; b.addEventListener("click", async () => {
    const msg = b.closest(".msg"), rb = msg.querySelector(".rawbox"); if (rb.innerHTML) { rb.innerHTML = ""; return; }
    const m = await api(`/api/sessions/${encodeURIComponent(state.session.key)}/messages/${encodeURIComponent(b.dataset.raw)}?full=1`);
    rb.innerHTML = `<div class="lbl">Raw stored record <button class="small" data-copy="${esc(JSON.stringify(m.raw, null, 2))}">copy</button></div><pre>${esc(JSON.stringify(m.raw, null, 2))}</pre>`; }); }
  for (const b of box.querySelectorAll("button.full:not([data-w])")) { b.dataset.w = 1; b.addEventListener("click", async () => {
    const full = await api(`/api/sessions/${encodeURIComponent(state.session.key)}/messages/${encodeURIComponent(b.dataset.full)}?full=1`);
    const i = state.list.findIndex(x => x.id === full.id); delete full.raw; state.list[i] = full; state.rendered.delete(full.id); renderTranscript(); }); }
}

// ---------- sources / diagnostics ----------
async function showSources() {
  const d = await api("/api/sources"); state.sources = d.sources;
  const when = t => t ? new Date(t * 1000).toLocaleString() : "never";
  app.innerHTML = `<h2>Sources</h2><p class="muted">Running on <strong>${esc(d.platform)}</strong>. Cache: <span class="mono">${esc(d.cache_dir)}</span>. Search index: ${d.indexing.running ? `building ${d.indexing.done}/${d.indexing.total}` : "up to date"}.</p>` +
    d.sources.map(s => `<div class="card"><h3 style="margin:0">${esc(s.label)} <span class="badge ${s.status === "ok" ? "b-ok" : s.status === "degraded" ? "b-warn" : "b-err"}">${esc(s.status)}</span></h3>
     <dl class="kv"><dt>ID / type</dt><dd>${esc(s.id)} · ${esc(s.type)} · ${esc(s.origin)}</dd><dt>Data path</dt><dd class="mono">${esc(s.root)}</dd>
     <dt>Sessions</dt><dd>${fmtN(s.session_count)} <span class="muted">(indexed for search: ${fmtN(s.indexed)})</span></dd>
     <dt>OpenCode version</dt><dd>executable: ${esc(s.executable_version || "unknown")} <span class="muted">(${esc(s.executable_note || "")})</span><br>recorded in sessions: ${esc(Object.entries(s.recorded_versions || {}).map(([v, n]) => `${v} ×${n}`).join(", ") || "—")}</dd>
     <dt>Storage format</dt><dd>${(s.details || []).map(x => esc(x.format) + ": " + esc((x.session_models || x.layout || "").toString())).join("<br>") || "—"}${(s.details || []).map(x => x.tables_present ? `<br><span class="muted">tables: ${esc(x.tables_present.join(", "))}${x.latest_migration ? " · latest migration " + esc(x.latest_migration) : ""}</span>` : "").join("")}</dd>
     <dt>Access mode</dt><dd>${esc(s.access_mode || "—")} ${(s.details || []).map(x => x.access_reason ? `<span class="muted">— ${esc(x.access_reason)}</span>` : "").join("")}</dd>
     <dt>Live status</dt><dd>${s.status_url ? esc(s.status_url) : '<span class="muted">not configured → session status shown as “unknown” (see README: --status-url)</span>'}</dd>
     <dt>Last refresh</dt><dd>${esc(when(s.last_refresh))} <span class="muted">(last attempt ${esc(when(s.last_attempt))})</span></dd>
     ${s.note ? `<dt>Note</dt><dd>${esc(s.note)}</dd>` : ""}${s.error ? `<dt>Error</dt><dd class="errbox">${esc(s.error)}</dd>` : ""}</dl></div>`).join("") +
    `<h2>WSL distributions</h2>${d.wsl_error ? `<div class="errbox">${esc(d.wsl_error)}</div>` : ""}` +
    (d.wsl_distributions.length ? `<table><thead><tr><th>Name</th><th>State</th><th>WSL</th><th>OpenCode data roots</th><th>Note</th></tr></thead><tbody>${d.wsl_distributions.map(x => `<tr><td>${esc(x.name)}${x.default ? " (default)" : ""}</td><td>${esc(x.state)}</td><td>${esc(x.version)}</td><td class="mono">${esc((x.roots || []).join(", ") || "—")}</td><td class="muted">${esc(x.note || "")}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">None detected.</p>');
}

// ---------- live refresh (low-frequency polling) ----------
state.stick = false;
async function poll() {
  try {
    const key = state.view === "session" && state.session ? state.session.key : "";
    const c = await api("/api/changes" + (key ? "?key=" + encodeURIComponent(key) : ""));
    updateIndexState(c.indexing);
    if (state.view === "list" && c.version !== state.version && !document.activeElement?.matches("input[type=search],input[type=date]")) { const y = window.scrollY; await loadList(); window.scrollTo(0, y); }
    else if (state.view === "session" && key && c.message_fingerprint !== state.fp) { state.fp = c.message_fingerprint; await loadMessages(false); }
    else if (state.view === "sources") await showSources();
  } catch (e) { /* server restarting or transiently busy: try again next tick */ }
  setTimeout(poll, 4000);
}
api("/api/sources").then(d => { state.sources = d.sources; }).catch(() => { });
route().then(() => setTimeout(poll, 4000));
