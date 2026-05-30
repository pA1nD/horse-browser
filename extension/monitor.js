// monitor.js — agent-surveillance console.
//
// This page is a *second* CDP client on :9223 (browser-harness is the first).
// Modern Chrome allows multiple flat sessions per target, so we can screencast a
// tab while an agent drives it. Discovery uses chrome.tabs + chrome.tabGroups:
// each tab-group is a "session" (one agent), listed in the sidebar; clicking a
// session filters the wall to just its tabs. The grid syncs in real time — panes
// appear/disappear as tabs open/close, and the ACTIVE tab in each window gets a
// coloured ring (a stable signal, unlike sporadic background frames).

const CDP = "http://127.0.0.1:9223";

const GROUP_COLORS = {
  grey: "#9aa0a6", blue: "#8ab4f8", red: "#f28b82", yellow: "#fdd663",
  green: "#81c995", pink: "#ff8bcb", purple: "#c58af9", cyan: "#78d9ec", orange: "#fcad70",
};

const grid = document.getElementById("grid");
const emptyEl = document.getElementById("empty");
const colsSel = document.getElementById("cols");
const sessionsEl = document.getElementById("sessions");
const statTabs = document.getElementById("stat-tabs");
const statSessions = document.getElementById("stat-sessions");
const collapseBtn = document.getElementById("collapse");
document.getElementById("refresh").addEventListener("click", () => location.reload());

let ws, msgId = 0;
const pending = new Map();          // request id → resolver
const sessionHandlers = new Map();  // CDP sessionId → frame handler
const panes = new Map();            // targetId → pane
let frameAspect = 16 / 10;          // live content aspect (w/h), refined from real frames
let selectedKey = null;             // sidebar filter: null = all sessions

function send(method, params, sessionId) {
  return new Promise((resolve) => {
    const id = ++msgId;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params: params || {}, sessionId }));
  });
}

async function connect() {
  const ver = await (await fetch(CDP + "/json/version")).json();
  ws = new WebSocket(ver.webSocketDebuggerUrl);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.sessionId && sessionHandlers.has(m.sessionId)) sessionHandlers.get(m.sessionId)(m);
  };
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
}

// Every real web tab (http/https/file), tagged with its session (tab-group) and host.
async function discover() {
  const groups = await chrome.tabGroups.query({});
  const gById = new Map(groups.map((g) => [g.id, g]));
  const tabs = await chrome.tabs.query({});
  const targets = await chrome.debugger.getTargets();
  const tgtByTab = new Map(targets.filter((t) => t.tabId).map((t) => [t.tabId, t.id]));
  const self = location.href;
  return tabs
    .filter((t) => tgtByTab.has(t.id) && /^(https?|file):/.test(t.url || "") && t.url !== self)
    .map((t) => {
      const g = gById.get(t.groupId);
      let host = "";
      try { host = new URL(t.url).hostname.replace(/^www\./, ""); } catch {}
      return {
        tabId: t.id, targetId: tgtByTab.get(t.id), title: t.title, url: t.url, host,
        active: !!t.active,
        sessionKey: g ? "g" + t.groupId : "ungrouped",
        label: g ? (g.title || "session") : "Ungrouped",
        color: g ? (GROUP_COLORS[g.color] || "#9aa0a6") : "#5b6470",
      };
    });
}

// ── sidebar: one card per session ──────────────────────────────────────────
function buildSessions(agents) {
  const map = new Map();
  for (const a of agents) {
    let s = map.get(a.sessionKey);
    if (!s) {
      s = { key: a.sessionKey, label: a.label, color: a.color,
            grouped: a.sessionKey !== "ungrouped", tabs: [], active: false, host: "" };
      map.set(a.sessionKey, s);
    }
    s.tabs.push(a);
    if (a.active) s.active = true;
  }
  for (const s of map.values()) {
    const rep = s.tabs.find((t) => t.active) || s.tabs[0];
    s.host = rep ? (rep.host || rep.title || "") : "";
  }
  return [...map.values()].sort((a, b) =>
    a.grouped === b.grouped ? a.label.localeCompare(b.label) : (a.grouped ? -1 : 1));
}

const sesEls = new Map(); // key → card element ('__all__' or a session key)

function makeSesCard(key, cls) {
  const el = document.createElement("button");
  el.className = "ses" + (cls ? " " + cls : "");
  el.dataset.key = key;
  el.innerHTML =
    '<span class="ses-body"><span class="ses-label"></span><span class="ses-host"></span></span>' +
    '<span class="ses-meta"><span class="ses-count"></span><span class="ses-live"></span></span>';
  el.addEventListener("click", () => {
    selectedKey = key === "__all__" ? null : (selectedKey === key ? null : key);
    syncSelected();
    reconcile(); // re-filter the wall
  });
  return el;
}

function syncSelected() {
  for (const [k, el] of sesEls)
    el.classList.toggle("selected", k === "__all__" ? selectedKey === null : selectedKey === k);
}

function renderSidebar(agents) {
  const sessions = buildSessions(agents);
  if (selectedKey && !sessions.some((s) => s.key === selectedKey)) selectedKey = null;

  const want = new Set(["__all__", ...sessions.map((s) => s.key)]);
  for (const [k, el] of [...sesEls]) if (!want.has(k)) { el.remove(); sesEls.delete(k); }

  let allEl = sesEls.get("__all__");
  if (!allEl) { allEl = makeSesCard("__all__", "all"); sessionsEl.appendChild(allEl); sesEls.set("__all__", allEl); }
  allEl.querySelector(".ses-label").textContent = "All sessions";
  allEl.querySelector(".ses-host").textContent = sessions.length + (sessions.length === 1 ? " session" : " sessions");
  allEl.querySelector(".ses-count").textContent = agents.length;
  allEl.classList.toggle("active", agents.some((a) => a.active));

  for (const s of sessions) {
    let el = sesEls.get(s.key);
    if (!el) { el = makeSesCard(s.key); sessionsEl.appendChild(el); sesEls.set(s.key, el); }
    el.style.setProperty("--c", s.color);
    el.querySelector(".ses-label").textContent = s.grouped ? "session " + s.label : "Ungrouped";
    el.querySelector(".ses-host").textContent = s.host;
    el.querySelector(".ses-count").textContent = s.tabs.length;
    el.classList.toggle("active", s.active);
  }
  // keep stable order (ALL first, then sessions); appendChild just moves nodes
  sessionsEl.appendChild(allEl);
  for (const s of sessions) sessionsEl.appendChild(sesEls.get(s.key));

  statTabs.textContent = agents.length;
  statSessions.textContent = sessions.length;
  syncSelected();
}

// ── screencast panes ────────────────────────────────────────────────────────
function draw(pane, b64) {
  const img = pane.img;
  img.onload = () => {
    const c = pane.canvas;
    if (c.width !== img.naturalWidth) { c.width = img.naturalWidth; c.height = img.naturalHeight; }
    pane.ctx.drawImage(img, 0, 0);
    // refine the cell aspect from the real frame (all tabs share the agent window)
    const a = img.naturalWidth / img.naturalHeight;
    if (a > 0.1 && Math.abs(a - frameAspect) / frameAspect > 0.02) { frameAspect = a; scheduleLayout(); }
  };
  img.src = "data:image/jpeg;base64," + b64;
}

// Force one frame even on a never-painted background tab so the pane isn't blank.
async function forceCapture(pane) {
  if (!pane.sid) return;
  try {
    const r = await send("Page.captureScreenshot", { format: "jpeg", quality: 50 }, pane.sid);
    if (r.result && r.result.data) draw(pane, r.result.data);
  } catch {}
}

function makePane(info) {
  const el = document.createElement("div");
  el.className = "pane";
  el.dataset.tid = info.targetId; // lets reconcile detect & drop orphaned duplicate panes
  el.style.setProperty("--accent", info.color);
  el.innerHTML = '<canvas></canvas><div class="tag"><span class="dot"></span><span class="t"></span></div>';
  el.querySelector(".dot").style.background = info.color;
  const ttlEl = el.querySelector(".t");
  ttlEl.textContent = info.host || info.title || info.url;
  el.addEventListener("click", async () => {
    await chrome.tabs.update(info.tabId, { active: true });
    const tab = await chrome.tabs.get(info.tabId);
    chrome.windows.update(tab.windowId, { focused: true });
  });
  grid.appendChild(el);
  const canvas = el.querySelector("canvas");
  return { el, ttlEl, canvas, ctx: canvas.getContext("2d"), img: new Image(), lastFrame: 0 };
}

async function watch(info) {
  const att = await send("Target.attachToTarget", { targetId: info.targetId, flatten: true });
  const sid = att.result && att.result.sessionId;
  if (!sid) return;
  const pane = makePane(info);
  pane.sid = sid;
  pane.tabId = info.tabId;
  panes.set(info.targetId, pane);
  sessionHandlers.set(sid, (m) => {
    if (m.method !== "Page.screencastFrame") return;
    pane.lastFrame = Date.now();
    draw(pane, m.params.data);
    send("Page.screencastFrameAck", { sessionId: m.params.sessionId }, sid);
  });
  await send("Page.enable", {}, sid);
  await send("Page.startScreencast",
    { format: "jpeg", quality: 50, maxWidth: 900, maxHeight: 560, everyNthFrame: 1 }, sid);
  forceCapture(pane);
}

function removePane(targetId) {
  const p = panes.get(targetId);
  if (!p) return;
  if (p.sid) {
    try { send("Page.stopScreencast", {}, p.sid); } catch {}
    try { send("Target.detachFromTarget", { sessionId: p.sid }); } catch {}
    sessionHandlers.delete(p.sid);
  }
  p.el.remove();
  panes.delete(targetId);
}

// Sync the grid to the live set of (filtered) agent tabs: add new, drop gone.
// Non-reentrant: watch() awaits an attach before it registers its pane, so two
// overlapping runs could both attach the SAME target — spawning a duplicate pane
// whose loser orphans and goes stale. The lock serialises runs; a request that
// arrives mid-flight is re-queued.
let reconcileBusy = false, reconcileQueued = false;
async function reconcile() {
  if (reconcileBusy) { reconcileQueued = true; return; }
  reconcileBusy = true;
  try {
    const agents = await discover();
    renderSidebar(agents); // sidebar always reflects ALL sessions
    const visible = selectedKey ? agents.filter((a) => a.sessionKey === selectedKey) : agents;
    const want = new Map(visible.map((a) => [a.targetId, a]));
    for (const tid of [...panes.keys()]) if (!want.has(tid)) removePane(tid);
    for (const a of visible) {
      if (!panes.has(a.targetId)) await watch(a);
      const p = panes.get(a.targetId);
      if (!p) continue;
      if (p.ttlEl) p.ttlEl.textContent = a.host || a.title || a.url;
      p.el.style.setProperty("--accent", a.color);
      const dot = p.el.querySelector(".dot"); if (dot) dot.style.background = a.color;
      p.el.classList.toggle("is-active", a.active);
    }
    // self-heal: remove any orphaned pane DOM (a superseded duplicate or leftover)
    for (const el of grid.querySelectorAll(".pane")) {
      const tracked = panes.get(el.dataset.tid);
      if (!tracked || tracked.el !== el) el.remove();
    }
    emptyEl.hidden = panes.size > 0;
    layout();
  } finally {
    reconcileBusy = false;
    if (reconcileQueued) { reconcileQueued = false; scheduleReconcile(); }
  }
}

let reconcileTimer;
function scheduleReconcile() { clearTimeout(reconcileTimer); reconcileTimer = setTimeout(reconcile, 250); }
for (const ev of [chrome.tabs.onCreated, chrome.tabs.onRemoved, chrome.tabs.onUpdated,
                  chrome.tabs.onMoved, chrome.tabs.onAttached, chrome.tabs.onDetached,
                  chrome.tabs.onActivated,
                  chrome.tabGroups.onCreated, chrome.tabGroups.onUpdated, chrome.tabGroups.onRemoved]) {
  ev.addListener(scheduleReconcile);
}
setInterval(reconcile, 3000); // safety net for anything the events miss

// ── responsive layout ───────────────────────────────────────────────────────
// Tile N panes — each at the content aspect — into the stage, choosing the column
// count that makes panes as LARGE as possible. Panes match the frame aspect, so
// the canvas fills them edge-to-edge (no letterbox) with the whole tab visible.
function layout() {
  const n = panes.size;
  if (!n) return;
  const gap = 3;
  const W = grid.clientWidth, H = grid.clientHeight;
  if (W < 2 || H < 2) return;
  const ar = frameAspect;
  const forced = colsSel.value === "auto" ? null : +colsSel.value;
  let best = null;
  for (let cols = 1; cols <= n; cols++) {
    if (forced && cols !== forced) continue;
    const rows = Math.ceil(n / cols);
    const cw = (W - (cols - 1) * gap) / cols;
    const ch = (H - (rows - 1) * gap) / rows;
    if (cw <= 0 || ch <= 0) continue;
    let w = cw, h = cw / ar;
    if (h > ch) { h = ch; w = ch * ar; }
    const area = w * h;
    if (!best || area > best.area) best = { cols, w, h, area };
  }
  if (!best) return;
  grid.style.gridTemplateColumns = `repeat(${best.cols}, ${Math.floor(best.w)}px)`;
  grid.style.gridAutoRows = `${Math.floor(best.h)}px`;
}
let layoutTimer;
function scheduleLayout() { clearTimeout(layoutTimer); layoutTimer = setTimeout(layout, 120); }
colsSel.addEventListener("change", layout);
window.addEventListener("resize", layout);

// ── sidebar collapse (persisted) ─────────────────────────────────────────────
const COLLAPSE_KEY = "hb-monitor-collapsed";
const logoEl = document.querySelector(".logo");
function setCollapsed(c) {
  document.body.classList.toggle("collapsed", c);
  localStorage.setItem(COLLAPSE_KEY, c ? "1" : "0");
  layout();
  setTimeout(layout, 220); // after the width transition settles
}
if (localStorage.getItem(COLLAPSE_KEY) === "1") document.body.classList.add("collapsed");
collapseBtn.addEventListener("click", () => setCollapsed(!document.body.classList.contains("collapsed")));
logoEl.addEventListener("click", () => { if (document.body.classList.contains("collapsed")) setCollapsed(false); });

// quietly refresh thumbnails for tabs that aren't streaming frames (no blink)
setInterval(() => {
  const now = Date.now();
  for (const p of panes.values())
    if (now - p.lastFrame > 4000 && now - (p.lastPing || 0) > 5000) { p.lastPing = now; forceCapture(p); }
}, 2000);

(async () => {
  layout();
  let ok = false;
  for (let i = 0; i < 60 && !ok; i++) {
    try { await connect(); ok = true; }
    catch { statTabs.textContent = "…"; await new Promise((r) => setTimeout(r, 1000)); }
  }
  if (!ok) { statTabs.textContent = "—"; return; }
  ws.onclose = () => setTimeout(() => location.reload(), 1500); // browser restart → reconnect
  await reconcile();
})();
