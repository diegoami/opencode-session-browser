"use strict";
// Session tracker: one card per session id, polled live. Independent of the session list UI.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmmss = ms => ms ? new Date(ms).toLocaleTimeString([], { hour12: false }) : "";
const ago = ms => { if (!ms) return "?"; const s = Math.max(0, (Date.now() - ms) / 1000); return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${(s / 3600).toFixed(1)} h ago` : new Date(ms).toLocaleString(); };
const dur = ms => ms == null ? "" : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60000).toFixed(1)} min`;
const fmtTok = t => { if (!t) return ""; const n = (t.input || 0) + (t.output || 0) + (t.reasoning || 0); return n ? `${n.toLocaleString()} tok` : ""; };

const cards = new Map();      // tracked by id
const actCards = new Map();   // logs opened from the active list (not persisted)      // id -> {id, el, fp, data, paused, n, open:Set}
let timer = null;

function save() {
  const ids = [...cards.keys()];
  history.replaceState(null, "", ids.length ? "#" + ids.map(encodeURIComponent).join(",") : location.pathname);
  try { localStorage.setItem("track.ids", JSON.stringify(ids)); } catch { /* private window */ }
}

function add(id, map = cards, container = $("#cards")) {
  id = id.trim();
  if (!id || map.has(id)) return;
  const el = document.createElement("section");
  el.className = "card";
  el.innerHTML = `<div class="hd"><div class="title mono">${esc(id)}</div><div class="meta">loading…</div>
    <div class="bar"><span class="pills"></span><span class="sp"></span>
      <select class="n" title="steps shown"><option value="20">last 20</option><option value="50">last 50</option><option value="200">last 200</option><option value="0">all</option></select>
      <button class="pause" title="stop polling this session">pause</button>
      <button class="copy" title="copy session id">copy id</button>
      <button class="rm" title="stop tracking">✕</button></div></div>
    <div class="feed"></div>`;
  const c = { map, id, el, fp: null, data: null, paused: false, n: 20, open: new Set() };
  el.querySelector(".n").onchange = e => { c.n = +e.target.value; renderFeed(c, true); };
  el.querySelector(".pause").onclick = e => { c.paused = !c.paused; e.target.textContent = c.paused ? "resume" : "pause"; if (!c.paused) poll(c); };
  el.querySelector(".copy").onclick = () => navigator.clipboard?.writeText(c.data?.id || id);
  el.querySelector(".rm").onclick = () => { map.delete(id); el.remove(); if (map === cards) { save(); empty(); } else renderActive(); };
  el.querySelector(".feed").onclick = e => {
    const st = e.target.closest(".step"); if (!st || window.getSelection().toString()) return;
    const i = +st.dataset.i; c.open.has(i) ? c.open.delete(i) : c.open.add(i); st.classList.toggle("open");
  };
  el.querySelector(".meta").onclick = e => { const a = e.target.closest("a[data-id]"); if (a) { add(a.dataset.id); save(); } };
  map.set(id, c);
  container.querySelector(".empty")?.remove();
  container.appendChild(el);
  poll(c);
}

function empty() {
  if (!cards.size) $("#cards").innerHTML = `<p class="empty">Enter a session id above to track it.</p>`;
}

async function poll(c) {
  if (c.busy) return;
  c.busy = true;
  try {
    const r = await fetch(`/api/track?id=${encodeURIComponent(c.id)}` + (c.fp ? `&fp=${encodeURIComponent(c.fp)}` : ""));
    const d = await r.json();
    if (c.map.get(c.id) !== c) return;
    if (!r.ok) throw new Error(d.error || r.status);
    if (!d.found) { c.data = null; c.fp = null; renderMissing(c, d.message); return; }
    const changed = !d.unchanged;
    if (changed) { c.steps = d.steps; c.inProgress = d.in_progress; c.fp = d.fingerprint; }
    c.data = d;
    renderHead(c);
    if (changed) renderFeed(c, false);
  } catch (err) {
    c.el.querySelector(".meta").innerHTML = `<span class="msg err">${esc(err.message || err)}</span>`;
  } finally { c.busy = false; }
}

function renderMissing(c, msg) {
  c.el.querySelector(".meta").textContent = "";
  c.el.querySelector(".pills").innerHTML = `<span class="pill idle">waiting</span>`;
  c.el.querySelector(".feed").innerHTML = `<div class="msg">${esc(msg)}</div>`;
}

function renderHead(c) {
  const d = c.data, s = d.session;
  const model = [s.provider, s.model].filter(Boolean).join("/") + (s.variant ? ` (${s.variant})` : "");
  c.el.querySelector(".title").textContent = [s.title || "(untitled)", model, s.directory].filter(Boolean).join(" · ");
  const bits = [`<span class="mono">${esc(d.id)}</span>`, esc(s.source) + (d.also_in.length ? ` (also in ${esc(d.also_in.join(", "))})` : ""),
    s.agent ? `agent ${esc(s.agent)}` : "", `updated ${esc(ago(s.updated))}`,
    s.message_count != null ? `${s.message_count} messages` : "", esc(fmtTok(s.tokens)), s.cost ? `$${s.cost.toFixed(4)}` : ""];
  if (s.parent) bits.push(`parent: <a data-id="${esc(s.parent.id)}" title="track parent">${esc(s.parent.title || s.parent.id)}</a>`);
  if (s.children.length) bits.push(`sub-sessions: ` + s.children.map(k => `<a data-id="${esc(k.id)}" title="track ${esc(k.id)}">${esc(k.title || k.id)}</a>`).join(", "));
  c.el.querySelector(".meta").innerHTML = bits.filter(Boolean).join(" · ");
  const pills = [];
  if (c.inProgress) pills.push(`<span class="pill run" title="from the stored data">● ${esc(c.inProgress)}</span>`);
  else pills.push(`<span class="pill idle">idle</span>`);
  if (s.status && s.status !== "unknown") pills.push(`<span class="pill run" title="${esc(s.status_basis)}">server: ${esc(s.status)}</span>`);
  if (s.outcome) pills.push(`<span class="pill ${s.outcome === "succeeded" ? "ok" : "bad"}" title="last stored outcome">${esc(s.outcome)}</span>`);
  if (c.paused) pills.push(`<span class="pill idle">paused</span>`);
  c.el.querySelector(".pills").innerHTML = pills.join(" ");
}

function stepHtml(st, i, open) {
  const cls = `step ${st.kind}${open ? " open" : ""}`;
  let body;
  if (st.kind === "tool") {
    body = `<span class="k k-tool">tool ${esc(st.tool)}</span> [<span class="st-${esc(st.status)}">${esc(st.status)}</span>]` +
      (st.duration_ms != null ? ` <span class="t">${dur(st.duration_ms)}</span>` : "") + `: ${esc(st.input)}` +
      (st.error ? ` <span class="k-error">— ${esc(st.error)}</span>` : "") +
      (st.child_session_id ? ` <a data-id="${esc(st.child_session_id)}">→ ${esc(st.child_session_id)}</a>` : "");
  } else {
    body = `<span class="k k-${esc(st.kind)}">${esc(st.kind)}:</span> ${esc(st.text)}`;
  }
  return `<div class="${cls}" data-i="${i}"><span class="t">${hhmmss(st.time)}</span><span class="b">${body}</span></div>`;
}

function renderFeed(c, force) {
  const feed = c.el.querySelector(".feed");
  if (!c.steps) return;
  const atBottom = force || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  const thinking = $("#thinking").checked;
  let list = c.steps.map((st, i) => [st, i]).filter(([st]) => thinking || st.kind !== "thinks");
  if (c.n) list = list.slice(-c.n);
  const hidden = c.steps.length - list.length;
  feed.innerHTML = (hidden ? `<div class="msg">${hidden} earlier step(s) not shown</div>` : "") +
    (list.length ? list.map(([st, i]) => stepHtml(st, i, c.open.has(i))).join("") : `<div class="msg">no steps yet</div>`);
  feed.querySelectorAll("a[data-id]").forEach(a => a.onclick = e => { e.stopPropagation(); add(a.dataset.id); save(); });
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => {
    for (const c of [...cards.values(), ...actCards.values()]) if (!c.paused) poll(c);
    if (tab === "act") loadActive();
  }, +$("#poll").value * 1000);
}

$("#add").addEventListener("submit", e => {
  e.preventDefault();
  for (const id of $("#ids").value.split(/[\s,;]+/)) add(id);
  $("#ids").value = "";
  save();
});
$("#thinking").onchange = () => { for (const c of [...cards.values(), ...actCards.values()]) renderFeed(c, false); };
$("#poll").onchange = schedule;

let initial = location.hash.slice(1).split(",").map(decodeURIComponent).filter(Boolean);
if (!initial.length) { try { initial = JSON.parse(localStorage.getItem("track.ids") || "[]"); } catch { initial = []; } }
initial.forEach(id => add(id));
save();
schedule();

// ---- Active sessions tab: recently written / open sessions, with a show/hide toggle for each log ----
let tab = "act", activeData = null;
function setTab(t) {
  tab = t;
  $("#tab-id").classList.toggle("on", t === "id"); $("#tab-act").classList.toggle("on", t === "act");
  $("#cards").hidden = t !== "id"; $("#active").hidden = t !== "act"; $("#actopt").hidden = t !== "act"; $("#finopt").hidden = t !== "act"; $("#scropt").hidden = t !== "act"; $("#add").hidden = t !== "id";
  if (t === "act") loadActive();
}
async function loadActive() {
  try {
    const r = await fetch(`/api/active?minutes=${$("#mins").value}&hide_scratch=${$("#hidescr").checked ? 1 : 0}`);
    activeData = await r.json();
    if (!r.ok) throw new Error(activeData.error || r.status);
    renderActive();
  } catch (err) { if (!activeData) $("#active").innerHTML = `<p class="empty">${esc(err.message || err)}</p>`; }
}
function renderActive() {
  const box = $("#active"), d = activeData;
  if (!d) return;
  const hideFin = $("#hidefin").checked;
  const isDone = s => !s.in_progress && s.status !== "busy";
  const shown = d.sessions.filter(s => !(hideFin && isDone(s)));
  const ids = new Set(shown.map(s => s.id));
  for (const [id, c] of actCards) if (!ids.has(id) && !c.keep) { actCards.delete(id); c.el.closest(".arow")?.remove(); }
  if (!shown.length) { actCards.clear(); box.innerHTML = `<p class="empty">${d.sessions.length ? "All sessions in this window are finished." : `No session written to in the last ${d.minutes} min.`}</p>`; return; }
  box.querySelector(".empty")?.remove();
  let prev = null;
  for (const s of shown) {
    let row = box.querySelector(`.arow[data-id="${CSS.escape(s.id)}"]`);
    if (!row) {
      row = document.createElement("div");
      row.className = "arow"; row.dataset.id = s.id;
      row.innerHTML = `<div class="ah"><span class="pills"></span><div class="tt"><div class="dir mono"></div><b></b><small></small></div><div class="last"></div><button class="log"></button></div>`;
      row.querySelector(".log").onclick = () => {
        const c = actCards.get(s.id);
        if (c) { actCards.delete(s.id); c.el.remove(); } else { add(s.id, actCards, row); }
        syncBtn(row, s.id);
      };
      box.appendChild(row);
    }
    const model = [s.provider, s.model].filter(Boolean).join("/");
    row.querySelector("b").textContent = s.title || "(untitled)";
    row.querySelector(".dir").textContent = s.directory || "(no directory)";
    row.querySelector("small").textContent = [s.id, model, s.source].filter(Boolean).join(" · ");
    const age = s.updated ? (Date.now() - s.updated) / 1000 : Infinity;
    const lastEl = row.querySelector(".last");
    lastEl.className = "last " + (age < 60 ? "fresh" : age < 600 ? "recent" : "stale");
    lastEl.innerHTML = `<span class="lbl">last log</span><b>${esc(ago(s.updated))}</b><span class="abs">${esc(hhmmss(s.updated))}</span>`;
    row.querySelector(".pills").innerHTML = s.in_progress ? `<span class="pill run">● ${esc(s.in_progress)}</span>` : s.status === "busy" ? `<span class="pill run">server: busy</span>` : `<span class="pill idle">idle</span>`;
    syncBtn(row, s.id);
    if (prev ? prev.nextElementSibling !== row : box.firstElementChild !== row) box.insertBefore(row, prev ? prev.nextElementSibling : box.firstElementChild);
    prev = row;
  }
}
function syncBtn(row, id) { row.querySelector(".log").textContent = actCards.has(id) ? "hide log" : "show log"; }
$("#tab-id").onclick = () => setTab("id");
$("#tab-act").onclick = () => setTab("act");
$("#mins").onchange = loadActive;
try { $("#hidescr").checked = localStorage.getItem("track.hidescr") !== "0"; } catch { /* ignore */ }
$("#hidescr").onchange = () => { try { localStorage.setItem("track.hidescr", $("#hidescr").checked ? "1" : "0"); } catch { /* ignore */ } loadActive(); };
try { $("#hidefin").checked = localStorage.getItem("track.hidefin") === "1"; } catch { /* ignore */ }
$("#hidefin").onchange = () => { try { localStorage.setItem("track.hidefin", $("#hidefin").checked ? "1" : "0"); } catch { /* ignore */ } renderActive(); };
setTab("act");
