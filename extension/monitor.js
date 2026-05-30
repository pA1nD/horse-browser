// monitor.js — Horse Browser console.
//
// This page is a *second* CDP client on :9223 (browser-harness is the first).
// Modern Chrome allows multiple flat sessions per target, so we can screencast a
// tab while an agent drives it. The SIDEBAR lists every web tab ranked by most
// recent activity (with a relative "active / 5m ago" time). The GRID is a fixed
// 2×2 / 3×3 wall showing the N² most-recently-active tabs, live.

const CDP = "http://127.0.0.1:9223";

const GROUP_COLORS = {
  grey: "#9aa0a6", blue: "#8ab4f8", red: "#f28b82", yellow: "#fdd663",
  green: "#81c995", pink: "#ff8bcb", purple: "#c58af9", cyan: "#78d9ec", orange: "#fcad70",
};

const grid = document.getElementById("grid");
const emptyEl = document.getElementById("empty");
const gridSel = document.getElementById("gridsel");
const tabListEl = document.getElementById("tablist");
const statTabs = document.getElementById("stat-tabs");
const collapseBtn = document.getElementById("collapse");

let ws, msgId = 0;
const pending = new Map();          // request id → resolver
const sessionHandlers = new Map();  // CDP sessionId → frame handler
const panes = new Map();            // targetId → pane
let frameAspect = 16 / 10;          // live content aspect (w/h), refined from real frames

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

// ── per-tab activity tracking ────────────────────────────────────────────────
// lastActivity[tabId] = ms of the most recent action we could observe: a
// navigation / load / title / favicon change (fires for background tabs too, so
// it catches agent-driven navigations), a screencast repaint, a focus, or the
// tab's own lastAccessed as a seed for history before the monitor opened.
const lastActivity = new Map();
function noteActivity(tabId, ts) {
  if (tabId == null) return;
  if (ts > (lastActivity.get(tabId) || 0)) lastActivity.set(tabId, ts);
}
chrome.tabs.onUpdated.addListener((tabId, ci) => {
  if (ci.url || ci.status || ci.title || ci.favIconUrl || ci.audible !== undefined) noteActivity(tabId, Date.now());
});
chrome.tabs.onActivated.addListener(({ tabId }) => noteActivity(tabId, Date.now()));
chrome.tabs.onCreated.addListener((t) => noteActivity(t.id, Date.now()));

function ago(ts) {
  if (!ts) return "—";
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60) return "active";
  const m = Math.floor(s / 60); if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60); if (h < 24) return h + "h ago";
  return Math.floor(h / 24) + "d ago";
}

// Every real web tab (http/https/file), with its observed last-activity time.
async function discover() {
  const groups = await chrome.tabGroups.query({});
  const gById = new Map(groups.map((g) => [g.id, g]));
  const tabs = await chrome.tabs.query({});
  const targets = await chrome.debugger.getTargets();
  const tgtByTab = new Map(targets.filter((t) => t.tabId).map((t) => [t.tabId, t.id]));
  const self = location.href;
  const out = tabs
    .filter((t) => tgtByTab.has(t.id) && /^(https?|file):/.test(t.url || "") && t.url !== self)
    .map((t) => {
      const g = gById.get(t.groupId);
      let host = ""; try { host = new URL(t.url).hostname.replace(/^www\./, ""); } catch {}
      if (!lastActivity.has(t.id)) lastActivity.set(t.id, t.lastAccessed || Date.now());
      else if (t.lastAccessed && t.lastAccessed > lastActivity.get(t.id)) lastActivity.set(t.id, t.lastAccessed);
      return {
        tabId: t.id, targetId: tgtByTab.get(t.id), title: t.title, url: t.url, host,
        favIconUrl: t.favIconUrl || "", active: !!t.active,
        color: g ? (GROUP_COLORS[g.color] || "#9aa0a6") : "#5b6470",
        lastActivity: lastActivity.get(t.id),
      };
    });
  // prune activity for tabs that no longer exist
  const live = new Set(out.map((a) => a.tabId));
  for (const k of [...lastActivity.keys()]) if (!live.has(k)) lastActivity.delete(k);
  return out;
}

// ── sidebar: one entry per tab, ranked by recency ────────────────────────────
const tabEls = new Map(); // targetId → entry element

function makeEntry(key) {
  const el = document.createElement("div");
  el.className = "tab";
  el.dataset.key = key;
  el.innerHTML =
    '<span class="tab-ico"><img alt="" /></span>' +
    '<span class="tab-body"><span class="tab-title"></span><span class="tab-host"></span></span>' +
    '<span class="tab-meta"><span class="tab-time"></span><span class="tab-live"></span></span>';
  el.querySelector("img").addEventListener("error", (e) => {
    e.target.removeAttribute("src"); e.target.closest(".tab-ico").classList.remove("has-ico");
  });
  return el;
}

function setIco(el, favIconUrl) {
  const ico = el.querySelector(".tab-ico");
  const img = ico.querySelector("img");
  if (favIconUrl && /^(https?:|data:)/.test(favIconUrl)) {
    if (img.getAttribute("src") !== favIconUrl) img.src = favIconUrl;
    ico.classList.add("has-ico");
  } else { img.removeAttribute("src"); ico.classList.remove("has-ico"); }
}

function renderSidebar(ranked) {
  const want = new Set(ranked.map((a) => a.targetId));
  for (const [k, el] of [...tabEls]) if (!want.has(k)) { el.remove(); tabEls.delete(k); }

  for (const a of ranked) {
    let el = tabEls.get(a.targetId);
    if (!el) { el = makeEntry(a.targetId); tabListEl.appendChild(el); tabEls.set(a.targetId, el); }
    el.style.setProperty("--c", a.color);
    setIco(el, a.favIconUrl);
    el.querySelector(".tab-title").textContent = a.title || a.host || a.url;
    el.querySelector(".tab-host").textContent = a.host;
    el.querySelector(".tab-time").textContent = ago(a.lastActivity);
    el.classList.toggle("active", Date.now() - a.lastActivity < 60000);
  }
  // Re-order to match the ranking, moving only out-of-place nodes (moving a node
  // restarts its entrance animation, so a steady ranking touches nothing).
  let node = tabListEl.firstChild;
  for (const a of ranked) {
    const el = tabEls.get(a.targetId);
    if (node === el) node = node.nextSibling;
    else tabListEl.insertBefore(el, node);
  }
  statTabs.textContent = ranked.length;
}

// ── screencast panes ─────────────────────────────────────────────────────────
function draw(pane, b64) {
  const img = pane.img;
  img.onload = () => {
    const c = pane.canvas;
    if (c.width !== img.naturalWidth) { c.width = img.naturalWidth; c.height = img.naturalHeight; }
    pane.ctx.drawImage(img, 0, 0);
    const a = img.naturalWidth / img.naturalHeight;
    if (a > 0.1 && Math.abs(a - frameAspect) / frameAspect > 0.02) { frameAspect = a; scheduleLayout(); }
  };
  img.src = "data:image/jpeg;base64," + b64;
}

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
    noteActivity(pane.tabId, Date.now()); // a repaint = activity on this tab
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

// Sync the grid to the N² most-recently-active tabs; sidebar shows them all.
// Non-reentrant: watch() awaits an attach before it registers its pane, so two
// overlapping runs could both attach the SAME target — spawning a duplicate pane
// whose loser orphans. The lock serialises runs; a request mid-flight re-queues.
let reconcileBusy = false, reconcileQueued = false;
async function reconcile() {
  if (reconcileBusy) { reconcileQueued = true; return; }
  reconcileBusy = true;
  try {
    const agents = await discover();
    const ranked = agents.slice().sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0));
    renderSidebar(ranked);
    const cap = (+gridSel.value || 2) ** 2;     // 2×2 → 4, 3×3 → 9
    const visible = ranked.slice(0, cap);
    const want = new Map(visible.map((a) => [a.targetId, a]));
    for (const tid of [...panes.keys()]) if (!want.has(tid)) removePane(tid);
    for (const a of visible) {
      if (!panes.has(a.targetId)) await watch(a);
      const p = panes.get(a.targetId);
      if (!p) continue;
      if (p.ttlEl) p.ttlEl.textContent = a.host || a.title || a.url;
      p.el.style.setProperty("--accent", a.color);
      const dot = p.el.querySelector(".dot"); if (dot) dot.style.background = a.color;
      p.el.classList.toggle("is-active", a.active); // focused tab in its window
    }
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
setInterval(reconcile, 3000); // safety net + keeps the relative times ticking

// ── fixed N×N layout ─────────────────────────────────────────────────────────
// Cells sized to the content aspect within an N-column, N-row grid, centred — so
// each pane fills edge-to-edge (no letterbox) and the wall is a tidy 2×2 / 3×3.
function layout() {
  const n = panes.size;
  if (!n) return;
  const N = +gridSel.value || 2;
  const gap = 3;
  const W = grid.clientWidth, H = grid.clientHeight;
  if (W < 2 || H < 2) return;
  const ar = frameAspect;
  const cw = (W - (N - 1) * gap) / N, ch = (H - (N - 1) * gap) / N;
  if (cw <= 0 || ch <= 0) return;
  let w = cw, h = cw / ar;
  if (h > ch) { h = ch; w = ch * ar; }
  grid.style.gridTemplateColumns = `repeat(${N}, ${Math.floor(w)}px)`;
  grid.style.gridAutoRows = `${Math.floor(h)}px`;
}
let layoutTimer;
function scheduleLayout() { clearTimeout(layoutTimer); layoutTimer = setTimeout(layout, 120); }
gridSel.addEventListener("change", reconcile); // grid size changes the cap → add/remove panes
window.addEventListener("resize", layout);

// ── sidebar collapse (persisted) ─────────────────────────────────────────────
const COLLAPSE_KEY = "hb-monitor-collapsed";
const logoEl = document.querySelector(".logo");
function setCollapsed(c) {
  document.body.classList.toggle("collapsed", c);
  localStorage.setItem(COLLAPSE_KEY, c ? "1" : "0");
  layout();
  setTimeout(layout, 220);
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
