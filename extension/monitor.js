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
  applyCols();
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

// Pick a column count that tiles all panes across the whole viewport with cells
// closest to a screencast frame's ~1.6 aspect — so the wall fills the screen.
function applyCols() {
  const n = Math.max(panes.size, 1);
  let cols;
  if (colsSel.value !== "auto") {
    cols = +colsSel.value;
  } else {
    cols = 1; let best = Infinity;
    const W = window.innerWidth, H = Math.max(window.innerHeight - 28, 1);
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c);
      const cellAR = (W / c) / (H / r);
      const score = Math.abs(Math.log(cellAR / 1.6));
      if (score < best) { best = score; cols = c; }
    }
  }
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  grid.style.gridAutoRows = "1fr";
}
colsSel.addEventListener("change", applyCols);
window.addEventListener("resize", applyCols);

// Quietly refresh thumbnails for tabs that aren't streaming frames (no visible
// state change, so nothing blinks). The active tab is shown via .is-active.
setInterval(() => {
  const now = Date.now();
  for (const p of panes.values())
    if (now - p.lastFrame > 4000 && now - (p.lastPing || 0) > 5000) { p.lastPing = now; forceCapture(p); }
}, 2000);

(async () => {
  applyCols();
  let ok = false;
  for (let i = 0; i < 60 && !ok; i++) {
    try { await connect(); ok = true; }
    catch { countEl.textContent = "connecting to CDP :9223…"; await new Promise((r) => setTimeout(r, 1000)); }
  }
  if (!ok) { countEl.textContent = "can't reach CDP on :9223"; return; }
  ws.onclose = () => setTimeout(() => location.reload(), 1500); // browser restart → reconnect
  await reconcile();
})();
