// monitor.js — CCTV grid of every agent's tabs.
//
// This page is a *second* CDP client on :9223 (browser-harness is the first).
// Modern Chrome allows multiple flat sessions per target, so we can screencast
// a tab while an agent drives it — verified, no conflict. Discovery of which
// tabs belong to which agent uses the extension's own chrome.tabGroups (each
// session = one coloured group, labelled with the last 4 chars of its id).

const CDP = "http://127.0.0.1:9223";
const IDLE_MS = 4000; // no frame for this long → pane goes "no activity"

// chrome tab-group colour names → CSS
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

function makePane(info) {
  const el = document.createElement("div");
  el.className = "pane";
  el.style.setProperty("--accent", info.color);
  el.innerHTML =
    '<div class="bar"><span class="lbl"></span><span class="ttl"></span></div>' +
    '<div class="screen"><canvas></canvas><div class="idle">no activity</div></div>';
  const lbl = el.querySelector(".lbl");
  lbl.textContent = info.label;
  lbl.style.background = info.color;
  el.querySelector(".ttl").textContent = info.title || info.url;
  el.querySelector(".screen").addEventListener("click", async () => {
    await chrome.tabs.update(info.tabId, { active: true });
    const tab = await chrome.tabs.get(info.tabId);
    chrome.windows.update(tab.windowId, { focused: true });
  });
  grid.appendChild(el);
  const canvas = el.querySelector("canvas");
  return { el, canvas, ctx: canvas.getContext("2d"), img: new Image(), lastFrame: 0 };
}

async function watch(info) {
  const att = await send("Target.attachToTarget", { targetId: info.targetId, flatten: true });
  const sid = att.result && att.result.sessionId;
  if (!sid) return;
  const pane = makePane(info);
  panes.set(info.targetId, pane);
  sessionHandlers.set(sid, (m) => {
    if (m.method !== "Page.screencastFrame") return;
    pane.lastFrame = Date.now();
    pane.el.classList.remove("is-idle");
    pane.img.onload = () => {
      const c = pane.canvas;
      if (c.width !== pane.img.naturalWidth) { c.width = pane.img.naturalWidth; c.height = pane.img.naturalHeight; }
      pane.ctx.drawImage(pane.img, 0, 0);
    };
    pane.img.src = "data:image/jpeg;base64," + m.params.data;
    send("Page.screencastFrameAck", { sessionId: m.params.sessionId }, sid);
  });
  await send("Page.enable", {}, sid);
  await send("Page.startScreencast",
    { format: "jpeg", quality: 50, maxWidth: 900, maxHeight: 560, everyNthFrame: 1 }, sid);
}

function applyCols() {
  const v = colsSel.value;
  grid.style.gridTemplateColumns =
    v === "auto" ? "repeat(auto-fit, minmax(360px, 1fr))" : `repeat(${v}, 1fr)`;
}
colsSel.addEventListener("change", applyCols);

// Dim panes that haven't produced a frame recently → "no activity".
setInterval(() => {
  const now = Date.now();
  for (const p of panes.values()) p.el.classList.toggle("is-idle", now - p.lastFrame > IDLE_MS);
}, 1000);

(async () => {
  applyCols();
  try { await connect(); }
  catch { countEl.textContent = "can't reach CDP on :9223"; return; }
  const agents = await discover();
  countEl.textContent = agents.length + (agents.length === 1 ? " tab" : " tabs");
  emptyEl.hidden = agents.length > 0;
  for (const a of agents) await watch(a);
})();
