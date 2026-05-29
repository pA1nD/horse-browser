// monitor.js — CCTV grid of every agent's tabs.
//
// This page is a *second* CDP client on :9223 (browser-harness is the first).
// Modern Chrome allows multiple flat sessions per target, so we can screencast
// a tab while an agent drives it — verified, no conflict. Discovery of which
// tabs belong to which agent uses the extension's own chrome.tabGroups (each
// session = one coloured group, labelled with the last 4 chars of its id).
//
// The grid syncs in real time: panes appear when an agent opens a tab and
// disappear when it closes (chrome.tabs / tabGroups events + a safety poll).
// The grid shows every real web tab. The tab that's ACTIVE in its window gets a
// coloured highlight — a stable signal of "where the agent is", unlike screencast
// frames (which arrive sporadically for background tabs and made an idle overlay
// blink). Thumbnails refresh quietly in the background.

const CDP = "http://127.0.0.1:9223";

const GROUP_COLORS = {
  grey: "#9aa0a6", blue: "#8ab4f8", red: "#f28b82", yellow: "#fdd663",
  green: "#81c995", pink: "#ff8bcb", purple: "#c58af9", cyan: "#78d9ec", orange: "#fcad70",
};

const grid = document.getElementById("grid");
const countEl = document.getElementById("count");
const emptyEl = document.getElementById("empty");
const colsSel = document.getElementById("cols");
document.getElementById("refresh").addEventListener("click", () => location.reload());

let ws, msgId = 0;
const pending = new Map();          // request id → resolver
const sessionHandlers = new Map();  // sessionId → event handler
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

// Every real web tab (http/https/file). Grouped tabs get their session colour +
// label; ungrouped tabs get a neutral marker so they still show up.
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
      return {
        tabId: t.id, targetId: tgtByTab.get(t.id), title: t.title, url: t.url, active: !!t.active,
        label: g ? (g.title || "•") : "—",
        color: g ? (GROUP_COLORS[g.color] || "#9aa0a6") : "#5b6470",
      };
    });
}

function draw(pane, b64) {
  const img = pane.img;
  img.onload = () => {
    const c = pane.canvas;
    if (c.width !== img.naturalWidth) { c.width = img.naturalWidth; c.height = img.naturalHeight; }
    pane.ctx.drawImage(img, 0, 0);
    // Refine the grid's cell aspect from the real frame so panes match the tab
    // (all tabs share the agent window, so one aspect fits all). Relayout if it shifts.
    const a = img.naturalWidth / img.naturalHeight;
    if (a > 0.1 && Math.abs(a - frameAspect) / frameAspect > 0.02) { frameAspect = a; scheduleLayout(); }
  };
  img.src = "data:image/jpeg;base64," + b64;
}

// Force one frame even on a never-painted background tab, to keep the thumbnail
// from going stale. Doesn't touch lastFrame or any visible state.
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
  el.style.setProperty("--accent", info.color);
  el.innerHTML =
    '<canvas></canvas>' +
    '<div class="tag"><span class="dot"></span><span class="t"></span></div>';
  el.querySelector(".dot").style.background = info.color;
  const ttlEl = el.querySelector(".t");
  ttlEl.textContent = info.label + " · " + (info.title || info.url);
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
  forceCapture(pane); // immediate first frame so the pane isn't blank
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

// Sync the grid to the live set of agent tabs: add new, drop gone, refresh titles.
async function reconcile() {
  const agents = await discover();
  const want = new Map(agents.map((a) => [a.targetId, a]));
  for (const tid of [...panes.keys()]) if (!want.has(tid)) removePane(tid);
  for (const a of agents) {
    if (!panes.has(a.targetId)) await watch(a);
    const p = panes.get(a.targetId);
    if (!p) continue;
    if (p.ttlEl) p.ttlEl.textContent = a.label + " · " + (a.title || a.url);
    p.el.style.setProperty("--accent", a.color);
    const dot = p.el.querySelector(".dot"); if (dot) dot.style.background = a.color;
    p.el.classList.toggle("is-active", a.active); // highlight the focused tab in each window
  }
  countEl.textContent = panes.size + (panes.size === 1 ? " tab" : " tabs");
  emptyEl.hidden = panes.size > 0;
  layout();
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

// Responsive layout: tile N panes — each at the content's aspect ratio — into
// the available area, choosing the column count that makes the panes as LARGE as
// possible. Because every pane matches the frame aspect, the canvas fills it
// edge-to-edge (no letterbox) while showing the whole tab (no crop); the only
// slack is a centred margin around the grid, which this minimises.
function layout() {
  const n = panes.size;
  if (!n) return;
  const gap = 2;
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
    let w = cw, h = cw / ar;          // fit content AR inside the cell box
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

// Quietly refresh thumbnails for tabs that aren't streaming frames (no visible
// state change, so nothing blinks). The active tab is shown via .is-active.
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
    catch { countEl.textContent = "connecting to CDP :9223…"; await new Promise((r) => setTimeout(r, 1000)); }
  }
  if (!ok) { countEl.textContent = "can't reach CDP on :9223"; return; }
  ws.onclose = () => setTimeout(() => location.reload(), 1500); // browser restart → reconnect
  await reconcile();
})();
