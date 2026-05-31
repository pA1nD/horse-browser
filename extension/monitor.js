// monitor.js — Horse Browser console.
//
// This page is a *second* CDP client on :9223 (browser-harness is the first).
// Modern Chrome allows multiple flat sessions per target, so we can screencast a
// tab while an agent drives it.
//
// THE WALL IS A FIXED SET OF SLOTS. The grid is N² slots (2×2 / 3×3), row-major:
// slot 0 = top-left … slot N²-1 = bottom-right. A tab, once placed in a slot,
// STAYS in that slot — activity inside a shown tab never reorders the wall, it
// only lights the slot's "active" outline in place. The order changes only when
// MEMBERSHIP changes, governed by computeSlots():
//   • a closed tab frees its slot; the hottest waiting tab fills it (no guard)
//   • a hotter waiting tab evicts the COLDEST slot — but only once that slot has
//     been idle longer than HOT_MS, so a wall full of busy agents stays frozen
//     and changes at most once something idles.
// The sidebar mirrors this: slot order on top (numbered, hard-linked to cells),
// a divider, then the bench (everything not shown) ranked by recency.

const CDP = "http://127.0.0.1:9223";

// How long a slot stays "hot" after its last activity: it glows, and it is
// PROTECTED from eviction for this long. Tunable — we may try 5s / 10s / 15s.
const HOT_MS = 15000;

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
const panes = new Map();            // targetId → pane (only for tabs currently on the wall)
let slots = [];                     // slot index → targetId | null  (the persistent wall)
let frameAspect = 16 / 10;          // live content aspect (w/h), refined from real frames

// Every CDP request times out. Right after a browser restart the freshly-restored
// renderer sometimes never answers an attach/screencast call; without a timeout
// that promise hangs forever, and because reconcile() holds a non-reentrant lock
// across `await watch()`, the WHOLE monitor deadlocks (empty sidebar + grid until
// a manual reload). On timeout we resolve to a benign error shape so callers that
// read `.result` simply treat it as "no data" and move on.
function send(method, params, sessionId, timeoutMs = 8000) {
  return new Promise((resolve) => {
    const id = ++msgId;
    let done = false;
    const finish = (v) => { if (done) return; done = true; pending.delete(id); resolve(v); };
    pending.set(id, finish);
    const timer = setTimeout(() => finish({ error: { message: "timeout", method } }), timeoutMs);
    // wrap so clearing the timer happens whenever the real response arrives
    const wrapped = (v) => { clearTimeout(timer); finish(v); };
    pending.set(id, wrapped);
    try { ws.send(JSON.stringify({ id, method, params: params || {}, sessionId })); }
    catch (e) { clearTimeout(timer); finish({ error: { message: String(e), method } }); }
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
// lastActivity[tabId] = ms of the most recent ACTION we can observe: a navigation
// or load (chrome.tabs.onUpdated url/status — fires for background tabs too, so it
// catches agent-driven navigations), a focus (onActivated), or the tab's own
// lastAccessed as a seed for history before the monitor opened. We deliberately do
// NOT count screencast repaints or passive title/favicon churn — those keep a tab
// painting without any real action and would peg every visible tab to "active".
const lastActivity = new Map();
function noteActivity(tabId, ts) {
  if (tabId == null) return;
  if (ts > (lastActivity.get(tabId) || 0)) lastActivity.set(tabId, ts);
}
chrome.tabs.onUpdated.addListener((tabId, ci) => {
  if (ci.url || ci.status) noteActivity(tabId, Date.now()); // navigation / (re)load only
});
chrome.tabs.onActivated.addListener(({ tabId }) => noteActivity(tabId, Date.now()));
chrome.tabs.onCreated.addListener((t) => noteActivity(t.id, Date.now()));

function ago(ts) {
  if (!ts) return "—";
  const s = Math.floor((Date.now() - ts) / 1000);
  if ((Date.now() - ts) < HOT_MS) return "active";
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60); if (m < 60) return m + "m";
  const h = Math.floor(m / 60); if (h < 24) return h + "h";
  return Math.floor(h / 24) + "d";
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

// <computeSlots> — PURE slot assignment. No DOM, no CDP, no globals (except it
// reads HOT_MS via the `guard` arg). Unit-tested in Node by slicing this region
// out of the file and eval'ing it, so the test exercises the real shipped logic.
//   prev   : previous slots array (targetId|null per cell)
//   agents : [{ targetId, lastActivity }, ...]  (the live tab set)
//   cap    : number of slots (N²)
//   now    : Date.now()
//   guard  : ms a slot must be idle before it can be evicted (HOT_MS)
// → new slots array of length `cap`.
function computeSlots(prev, agents, cap, now, guard) {
  const byId = new Map(agents.map((a) => [a.targetId, a]));
  // resize: keep slot→tab bindings for indices that still exist; pad/truncate.
  const slots = new Array(cap).fill(null);
  for (let i = 0; i < Math.min(cap, prev.length); i++) slots[i] = prev[i];
  // prune closed tabs and any duplicate (safety) — a tab lives in at most one slot.
  const seen = new Set();
  for (let i = 0; i < cap; i++) {
    const id = slots[i];
    if (id == null) continue;
    if (!byId.has(id) || seen.has(id)) slots[i] = null;
    else seen.add(id);
  }
  const benchSorted = () => {
    const placed = new Set();
    for (const id of slots) if (id != null) placed.add(id);
    return agents.filter((a) => !placed.has(a.targetId))
                 .sort((x, y) => y.lastActivity - x.lastActivity);
  };
  // 1) fill EMPTY slots, lowest index first, with the hottest bench tab — no guard.
  for (let i = 0; i < cap; i++) {
    if (slots[i] != null) continue;
    const b = benchSorted();
    if (!b.length) break;
    slots[i] = b[0].targetId;
  }
  // 2) GUARDED swaps: the hottest bench tab evicts the COLDEST slot iff it is
  //    strictly hotter AND that slot has been idle longer than `guard`. Repeats
  //    until no warranted swap (bounded by cap — converges, can't oscillate:
  //    an evicted tab is the coldest, so it can't be hotter than a remaining slot).
  for (let loop = 0; loop < cap; loop++) {
    const b = benchSorted();
    if (!b.length) break;
    const hottest = b[0];
    let coldIdx = -1, coldAct = Infinity;
    for (let i = 0; i < cap; i++) {
      const id = slots[i];
      if (id == null) continue;
      const act = byId.get(id).lastActivity;
      if (act < coldAct) { coldAct = act; coldIdx = i; }
    }
    if (coldIdx < 0) break;
    if (hottest.lastActivity > coldAct && (now - coldAct) > guard) {
      slots[coldIdx] = hottest.targetId; // evict cold; newcomer inherits the exact slot
    } else break;
  }
  return slots;
}
// </computeSlots>

// The hottest bench tab that WANTS a slot but is blocked by the guard (its target
// slot is still hot). Marked "standing by" in the sidebar. null if none waiting.
function standbyId(slots, agents, byId, now, guard) {
  const placed = new Set();
  for (const id of slots) if (id != null) placed.add(id);
  const bench = agents.filter((a) => !placed.has(a.targetId))
                      .sort((x, y) => y.lastActivity - x.lastActivity);
  if (!bench.length) return null;
  let coldAct = Infinity;
  for (const id of slots) if (id != null) coldAct = Math.min(coldAct, byId.get(id).lastActivity);
  const h = bench[0];
  if (h.lastActivity > coldAct && (now - coldAct) <= guard) return h.targetId;
  return null;
}

// ── sidebar: slot order on top (numbered), divider, then bench by recency ──────
const tabEls = new Map(); // targetId → entry element

function makeEntry(key) {
  const el = document.createElement("div");
  el.className = "tab";
  el.dataset.key = key;
  el.innerHTML =
    '<span class="tab-ico"><img alt="" /></span>' +
    '<span class="tab-body"><span class="tab-title"></span><span class="tab-host"></span></span>' +
    '<span class="tab-meta"><span class="slot-no"></span><span class="tab-time"></span></span>';
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

function renderSidebar(slots, agents, byId, cap) {
  // slotNo: targetId → 1-based slot number (shown tabs only)
  const slotNo = new Map();
  slots.forEach((id, i) => { if (id != null) slotNo.set(id, i + 1); });
  const shown = slots.filter((id) => id != null).map((id) => byId.get(id)); // slot order
  const shownSet = new Set(shown.map((a) => a.targetId));
  const bench = agents.filter((a) => !shownSet.has(a.targetId))
                      .sort((x, y) => y.lastActivity - x.lastActivity);
  const order = [...shown, ...bench];
  const standby = standbyId(slots, agents, byId, Date.now(), HOT_MS);

  // drop rows for tabs that vanished
  const want = new Set(order.map((a) => a.targetId));
  for (const [k, el] of [...tabEls]) if (!want.has(k)) { el.remove(); tabEls.delete(k); }

  order.forEach((a, i) => {
    let el = tabEls.get(a.targetId);
    if (!el) { el = makeEntry(a.targetId); tabListEl.appendChild(el); tabEls.set(a.targetId, el); }
    const n = slotNo.get(a.targetId);
    el.style.setProperty("--c", a.color);
    setIco(el, a.favIconUrl);
    el.querySelector(".tab-title").textContent = a.title || a.host || a.url;
    el.querySelector(".tab-host").textContent = a.host;
    el.querySelector(".tab-time").textContent = ago(a.lastActivity);
    el.querySelector(".slot-no").textContent = n ? n : "";
    el.classList.toggle("on-grid", !!n);
    el.classList.toggle("active", (Date.now() - a.lastActivity) < HOT_MS);
    el.classList.toggle("standby", a.targetId === standby);
    // cutoff divider: under the last shown row, only when a bench exists below it
    el.classList.toggle("grid-edge", i === shown.length - 1 && bench.length > 0);
  });
  // reorder DOM minimally (moving a node restarts its entrance animation, so a
  // steady order touches nothing).
  let node = tabListEl.firstChild;
  for (const a of order) {
    const el = tabEls.get(a.targetId);
    if (node === el) node = node.nextSibling;
    else tabListEl.insertBefore(el, node);
  }
  statTabs.textContent = agents.length;
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
    // fromSurface:true captures even a backgrounded tab. startScreencast only
    // streams frames for the VISIBLE tab, so background panes (and every tab right
    // after a browser restart) are painted by this still capture, not the stream.
    const r = await send("Page.captureScreenshot",
      { format: "jpeg", quality: 50, fromSurface: true }, pane.sid);
    if (r.result && r.result.data) draw(pane, r.result.data);
  } catch {}
}

function makePane(info) {
  const el = document.createElement("div");
  el.className = "pane";
  el.dataset.tid = info.targetId; // lets reconcile detect & drop orphaned duplicate panes
  el.style.setProperty("--accent", info.color);
  el.innerHTML = '<canvas></canvas><div class="slot-badge"></div>';
  el.addEventListener("click", async () => {
    await chrome.tabs.update(info.tabId, { active: true });
    const tab = await chrome.tabs.get(info.tabId);
    chrome.windows.update(tab.windowId, { focused: true });
  });
  grid.appendChild(el);
  const canvas = el.querySelector("canvas");
  return { el, badge: el.querySelector(".slot-badge"),
           canvas, ctx: canvas.getContext("2d"), img: new Image(), lastFrame: 0 };
}

async function watch(info) {
  // Short timeout (not the default 8s): right after a browser restart the target
  // subsystem isn't ready to attach for ~1–3s and simply doesn't answer. Failing
  // fast lets the 1s poll retry and succeed once the browser settles, instead of
  // every pane stalling the full 8s backstop (which read as a ~20s cold start).
  const FAST = 2500;
  const att = await send("Target.attachToTarget", { targetId: info.targetId, flatten: true }, undefined, FAST);
  const sid = att.result && att.result.sessionId;
  if (!sid) return; // not ready yet — next poll retries
  const pane = makePane(info);
  pane.sid = sid;
  pane.tabId = info.tabId;
  panes.set(info.targetId, pane);
  sessionHandlers.set(sid, (m) => {
    if (m.method !== "Page.screencastFrame") return;
    pane.lastFrame = Date.now();
    draw(pane, m.params.data); // NB: a repaint is NOT activity — pages paint passively
    send("Page.screencastFrameAck", { sessionId: m.params.sessionId }, sid);
  });
  await send("Page.enable", {}, sid);
  // fire the first-paint still immediately (don't await) so the pane shows ASAP,
  // and kick off the screencast in parallel — neither blocks the other.
  forceCapture(pane); // still: fastest first paint; the 2s heartbeat repaints thereafter
  send("Page.startScreencast",
    { format: "jpeg", quality: 50, maxWidth: 900, maxHeight: 560, everyNthFrame: 1 }, sid);
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

// Reconcile the live tab set into the fixed-slot wall. Non-reentrant: watch()
// awaits an attach before it registers its pane, so two overlapping runs could
// both attach the SAME target — spawning a duplicate pane whose loser orphans.
// The lock serialises runs; a request mid-flight re-queues.
let reconcileBusy = false, reconcileQueued = false;
async function reconcile() {
  if (reconcileBusy) { reconcileQueued = true; return; }
  reconcileBusy = true;
  try {
    const agents = await discover();
    const byId = new Map(agents.map((a) => [a.targetId, a]));
    const N = +gridSel.value || 2;
    const cap = N * N;
    slots = computeSlots(slots, agents, cap, Date.now(), HOT_MS);

    // Render the sidebar FIRST — it's pure DOM (no CDP), so the tab list always
    // appears even if pane attaches are slow or failing after a restart.
    renderSidebar(slots, agents, byId, cap);
    statTabs.textContent = agents.length;

    const shownSet = new Set(slots.filter((id) => id != null));
    for (const tid of [...panes.keys()]) if (!shownSet.has(tid)) removePane(tid);

    // Attach all missing panes IN PARALLEL. Each watch() is ~3 serial CDP
    // round-trips; doing them one-at-a-time made a fresh 3×3 wall take ~30 trips
    // back-to-back. Concurrently, wall-clock ≈ the slowest single tab. The Map
    // key + orphan sweep already prevent duplicate panes, and the reconcile lock
    // means only one batch runs at a time, so parallel attach is safe.
    await Promise.all(slots.map((id) => (id != null && !panes.has(id))
      ? watch(byId.get(id)).catch(() => {}) : null));

    for (let i = 0; i < cap; i++) {
      const id = slots[i];
      if (id == null) continue;
      const a = byId.get(id);
      const p = panes.get(id);
      if (!p) continue;
      // pin the pane to its slot's grid cell (row-major). Recomputed each tick so a
      // grid-size change (slot index → different cell) repositions correctly.
      p.el.style.gridColumn = (i % N) + 1;
      p.el.style.gridRow = Math.floor(i / N) + 1;
      p.badge.textContent = i + 1;
      p.el.classList.toggle("is-active", (Date.now() - a.lastActivity) < HOT_MS);
    }
    // orphan sweep: drop any pane DOM that isn't the tracked pane for its tab
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
// 1s poll: re-runs the slot machine so an idling slot crosses the HOT_MS guard and
// any standing-by tab can enter promptly — and keeps the relative times ticking.
setInterval(reconcile, 1000);

// ── fixed N×N layout ─────────────────────────────────────────────────────────
// All N×N cells always exist (explicit template-rows/cols), so a tab keeps its
// cell even when other slots are empty. Cells are sized to the content aspect and
// the grid is centred, so each pane fills edge-to-edge with no letterbox.
function layout() {
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
  grid.style.gridTemplateRows = `repeat(${N}, ${Math.floor(h)}px)`;
}
let layoutTimer;
function scheduleLayout() { clearTimeout(layoutTimer); layoutTimer = setTimeout(layout, 120); }
// persist the grid size across reloads / browser restarts
const GRID_KEY = "hb-monitor-grid";
const savedGrid = localStorage.getItem(GRID_KEY);
if (savedGrid === "2" || savedGrid === "3") gridSel.value = savedGrid;
gridSel.addEventListener("change", () => { // grid size changes the cap → resize slots
  localStorage.setItem(GRID_KEY, gridSel.value);
  reconcile();
});
window.addEventListener("resize", layout);

// ── sidebar collapse (persisted) ─────────────────────────────────────────────
const COLLAPSE_KEY = "hb-monitor-collapsed";
const logoEl = document.querySelector(".logo");

// Re-run layout on every frame for `ms`, so the grid tracks the sidebar width as
// it animates — panes resize continuously instead of snapping between two sizes.
let layoutAnim = 0;
function animateLayout(ms) {
  cancelAnimationFrame(layoutAnim);
  const start = performance.now();
  const step = (now) => {
    layout();
    if (now - start < ms) layoutAnim = requestAnimationFrame(step);
  };
  layoutAnim = requestAnimationFrame(step);
}
function setCollapsed(c) {
  document.body.classList.toggle("collapsed", c);
  localStorage.setItem(COLLAPSE_KEY, c ? "1" : "0");
  animateLayout(340); // a touch longer than the .3s width transition, to catch the settle
}
if (localStorage.getItem(COLLAPSE_KEY) === "1") document.body.classList.add("collapsed");
collapseBtn.addEventListener("click", () => setCollapsed(!document.body.classList.contains("collapsed")));
logoEl.addEventListener("click", () => setCollapsed(!document.body.classList.contains("collapsed")));
// ⌘B / Ctrl+B toggles the sidebar (Chrome-native side-panel shortcut feel)
window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === "b" || e.key === "B")) {
    e.preventDefault();
    setCollapsed(!document.body.classList.contains("collapsed"));
  }
});

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
