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
// Idle panes show a grayed thumbnail (refreshed by an occasional forced frame)
// plus a "no activity" overlay, so the eye lands on the panes that are moving.

const CDP = "http://127.0.0.1:9223";
const IDLE_MS = 4000;      // no real frame for this long → pane reads "no activity"
const REPING_MS = 6000;    // how often to refresh an idle pane's thumbnail

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

// Agent tabs = tabs that live in a session tab-group, with their CDP targetId.
async function discover() {
  const groups = await chrome.tabGroups.query({});
  const gById = new Map(groups.map((g) => [g.id, g]));
  const tabs = await chrome.tabs.query({});
  const targets = await chrome.debugger.getTargets();
  const tgtByTab = new Map(targets.filter((t) => t.tabId).map((t) => [t.tabId, t.id]));
  return tabs
    .filter((t) => gById.has(t.groupId) && tgtByTab.has(t.id))
    .map((t) => {
      const g = gById.get(t.groupId);
      return {
        tabId: t.id, targetId: tgtByTab.get(t.id), title: t.title, url: t.url,
        label: g.title || "•", color: GROUP_COLORS[g.color] || "#9aa0a6",
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

// Force one frame even on a never-painted background tab. Updates the thumbnail
// WITHOUT touching lastFrame, so it doesn't fake "activity" — idle stays idle.
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
  el.innerHTML =
    '<canvas></canvas>' +
    '<div class="tag"><span class="dot"></span><span class="t"></span></div>' +
    '<div class="idle">no activity</div>';
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
    pane.el.classList.remove("is-idle");
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
    else { const p = panes.get(a.targetId); if (p.ttlEl) p.ttlEl.textContent = a.label + " · " + (a.title || a.url); }
  }
  countEl.textContent = panes.size + (panes.size === 1 ? " tab" : " tabs");
  emptyEl.hidden = panes.size > 0;
  applyCols();
}

let reconcileTimer;
function scheduleReconcile() { clearTimeout(reconcileTimer); reconcileTimer = setTimeout(reconcile, 250); }
for (const ev of [chrome.tabs.onCreated, chrome.tabs.onRemoved, chrome.tabs.onUpdated,
                  chrome.tabs.onMoved, chrome.tabs.onAttached, chrome.tabs.onDetached,
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

// Mark idle panes, and refresh their thumbnail occasionally so it isn't stale.
setInterval(() => {
  const now = Date.now();
  for (const p of panes.values()) {
    const idle = now - p.lastFrame > IDLE_MS;
    p.el.classList.toggle("is-idle", idle);
    if (idle && now - (p.lastPing || 0) > REPING_MS) { p.lastPing = now; forceCapture(p); }
  }
}, 1000);

(async () => {
  applyCols();
  try { await connect(); }
  catch { countEl.textContent = "can't reach CDP on :9223"; return; }
  await reconcile();
})();
