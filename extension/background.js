// Agent Tab Grouper — CDP-driven.
//
// A CDP client attaches to this service worker and calls self.groupTab /
// self.activateTab / self.listTabs with Runtime.evaluate (awaitPromise: true).
// chrome.debugger.getTargets() bridges CDP targetIds to chrome.tabs.Tab ids.
// Nothing here is specific to any one driver — it's a plain tab grouper.

// MV3 service workers sleep when idle and then vanish from Target.getTargets
// until something wakes them — so a dormant SW would make groupTab/listTabs
// (and the install smoke test) silently miss us. Two keepalives:
//   • a 30s no-op alarm resets the idle timer so we stay warm once running;
//   • waking on tab creation covers the gap before the first alarm tick — a
//     tab is created right before we're asked to group it, and at launch.
chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }));
chrome.alarms.onAlarm.addListener(() => {});
chrome.tabs.onCreated.addListener(() => {});

// One-time cleanup: an earlier build installed a dynamic declarativeNetRequest rule (id 1001)
// for the sec-ch-ua header. The wire half now lives in the static rules.json (kept in sync
// with the browser version by the launcher), so remove any leftover dynamic rule — a stale
// one would supersede the correct static rule. Safe no-op once it's gone.
try { chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [1001] }); } catch (e) {}

const COLORS = ["blue", "cyan", "green", "yellow", "orange", "red", "pink", "purple"];

function colorForName(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return COLORS[h % COLORS.length];
}

// ── Session codename ── a stable identity (emoji + colour + last-4) derived from the
// session id, so any companion tool that renders the same code — a terminal statusline,
// a dashboard — matches this tab group. 48 emoji grouped 6-per-colour; colour group =
// slot / 6. The colours are Chrome's 8 tab-group colours (the binding constraint).
// Hash = FNV-1a + a murmur3 finalizer over the FULL session id; mirror this exact
// function if you render the codename in another tool, so they stay identical.
const CODE_EMOJI = ["🔥","🍎","🍓","🍒","🌹","🐞","🦊","🍊","🦁","🐯","🥕","🏀","🍋","🌻","⭐","🐝","🍌","🐥","🐸","🍀","🌵","🐢","🌲","🐍","🐬","🌊","💎","🧊","🐳","💧","🐧","🫐","🦋","🌀","🌐","🐟","🦄","🍇","🔮","🐙","🍆","👾","🌸","🐷","🦩","🍑","🌷","🌺"];
const CODE_ORDER = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"];

function hash32(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  h ^= h >>> 16; h = Math.imul(h, 0x7feb352d); h ^= h >>> 15; h = Math.imul(h, 0x846ca68b); h ^= h >>> 16;
  return h >>> 0;
}

// id → { title: "🐍 0FDA", color: "green" }. A bare ≤4-char label (no session id)
// falls back to the old plain-label group, so nothing breaks if the caller is older.
function codename(id) {
  id = id || "";
  if (id.length <= 4) return { title: id, color: colorForName(id) };
  const slot = hash32(id) % CODE_EMOJI.length;
  return { title: CODE_EMOJI[slot] + " " + id.slice(-4).toUpperCase(), color: CODE_ORDER[Math.floor(slot / 6)] };
}

async function tabIdForTargetId(targetId) {
  const targets = await chrome.debugger.getTargets();
  const t = targets.find(x => x.id === targetId);
  if (!t || !t.tabId) throw new Error(`no tab for CDP target ${targetId}`);
  return t.tabId;
}

// `session` is the FULL session id (the caller passes CLAUDE_CODE_SESSION_ID); the
// codename (emoji + colour + last-4) is derived here so the browser owns its render.
self.groupTab = async (targetId, session) => {
  const tabId = await tabIdForTargetId(targetId);
  const tab = await chrome.tabs.get(tabId);
  const { title, color } = codename(session);
  const existing = await chrome.tabGroups.query({ windowId: tab.windowId, title });
  if (existing.length) {
    await chrome.tabs.group({ tabIds: [tabId], groupId: existing[0].id });
  } else {
    const gid = await chrome.tabs.group({
      tabIds: [tabId],
      createProperties: { windowId: tab.windowId },
    });
    await chrome.tabGroups.update(gid, { title, color, collapsed: false });
  }
  return tabId;
};

// chrome.tabs.update({active:true}) swaps the browser's visible tab without raising
// the app — replaces CDP Target.activateTarget, which calls [NSApp activate]
// and steals macOS focus while the agent works.
self.activateTab = async (targetId) => {
  const tabId = await tabIdForTargetId(targetId);
  await chrome.tabs.update(tabId, { active: true });
  return tabId;
};

// CDP targetId of the tab currently visible in each browser window. No CDP equivalent
// exists — targets carry no "visible" flag — which is why this stays even though nothing
// in the harness calls it: tests/e2e.sh uses it to prove a screenshot of a background tab
// leaves the viewer's visible tab untouched, and there is no other way to observe that.
self.activeTabTargets = async () => {
  const tabs = await chrome.tabs.query({ active: true });
  const targets = await chrome.debugger.getTargets();
  const idByTab = new Map(targets.filter(t => t.tabId).map(t => [t.tabId, t.id]));
  return tabs.map(t => idByTab.get(t.id)).filter(Boolean);
};

// Close tabs that belong to ENDED agent sessions — the leak the operator sees as a pile of
// stray about:blank (and abandoned work) tabs. Each session's tabs live in a group titled by
// codename(session); `liveSessions` is the raw session ids still running (the launcher gathers
// them from live daemons). Any GROUPED tab whose group title isn't a live session's codename
// is an orphan and gets closed. Ungrouped tabs are left alone (not session-owned). Refuses to
// act on an empty live set (would look like everything is dead) — a hard safety stop.
self.reapDeadTabs = async (liveSessions) => {
  if (!Array.isArray(liveSessions) || liveSessions.length === 0) return { closed: 0, skipped: "no-live-sessions" };
  const liveTitles = new Set(liveSessions.map(s => codename(s).title));
  const tabs = await chrome.tabs.query({});
  const groups = await chrome.tabGroups.query({});
  const titleByGid = new Map(groups.map(g => [g.id, g.title]));
  const orphan = (t) => {
    if (t.pinned) return false;                                   // never the pinned monitor
    if (t.groupId >= 0) return !liveTitles.has(titleByGid.get(t.groupId));  // dead session's group
    return (t.url || "") === "" || t.url === "about:blank";       // ungrouped stray blank (daemon leak)
  };
  const dead = tabs.filter(orphan);
  for (const t of dead) { try { await chrome.tabs.remove(t.id); } catch {} }
  return { closed: dead.length };
};

self.listTabs = async (session) => {
  const { title } = codename(session);
  const groups = await chrome.tabGroups.query({ title });
  if (!groups.length) return [];
  const groupIds = new Set(groups.map(g => g.id));
  const tabs = await chrome.tabs.query({});
  const targets = await chrome.debugger.getTargets();
  const targetByTabId = new Map(targets.filter(t => t.tabId).map(t => [t.tabId, t.id]));
  return tabs
    .filter(t => groupIds.has(t.groupId))
    .map(t => ({
      targetId: targetByTabId.get(t.id) ?? null,
      tabId: t.id,
      url: t.url,
      title: t.title,
      lastAccessed: t.lastAccessed ?? null,
      discarded: !!t.discarded,
      audible: !!t.audible,
      active: !!t.active,
    }));
};

// The Agent Monitor — a CCTV grid of every session's live tabs. It's a normal extension
// page that does its own CDP work as a second client on this browser's debug port (which the
// launcher seeds into chrome.storage.local — see monitor.js). We keep exactly one, pinned
// as the first tab, "permanently": Chrome already shields pinned tabs from "Close other
// tabs", and showMonitor() reopens it if it's closed any other way. Defined as a const arrow
// (not a function declaration) right after MONITOR_URL — both bind reliably in the worker.
const MONITOR_URL = chrome.runtime.getURL("monitor.html");
const showMonitor = async (focus) => {
  const tabs = await chrome.tabs.query({ url: MONITOR_URL });
  if (tabs.length === 0) {
    await chrome.tabs.create({ url: MONITOR_URL, pinned: true, active: !!focus, index: 0 });
    return;
  }
  if (!tabs[0].pinned) await chrome.tabs.update(tabs[0].id, { pinned: true });
  if (focus) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
  }
  // de-dup: keep one Monitor (a closure burst can briefly mint two before either query returns)
  for (const t of tabs.slice(1)) { try { await chrome.tabs.remove(t.id); } catch (e) {} }
};

// ── which browser is this? — the toolbar badge answers it ───────────────────
// Several horse-browsers are open at once, one per agent, and they look identical: the Monitor
// is a PINNED tab, and a pinned tab shows only its favicon, so there is nowhere else on screen
// that names the instance. The badge carries the debug port the launcher seeded (see
// bin/horse-browser: seed_instance_env), which is the one number that identifies a browser —
// the tooltip spells it out for ports too long to fit. Colour is derived from the port, so two
// windows side by side differ at a glance rather than by reading digits.
const BADGE_COLORS = ["#1a73e8", "#12a150", "#c5221f", "#8430ce", "#b06000", "#00838f"];
async function showInstanceBadge() {
  const { hbCdpPort: port } = await chrome.storage.local.get("hbCdpPort");
  if (!port) {   // not seeded (yet) — say nothing rather than guess
    await chrome.action.setBadgeText({ text: "" });
    await chrome.action.setTitle({ title: "Open Agent Monitor" });
    return;
  }
  await chrome.action.setBadgeText({ text: String(port) });
  await chrome.action.setBadgeBackgroundColor({ color: BADGE_COLORS[port % BADGE_COLORS.length] });
  try { await chrome.action.setBadgeTextColor({ color: "#ffffff" }); } catch (e) {}  // Chrome ≥110
  await chrome.action.setTitle({ title: `Agent Monitor — this browser is CDP :${port}` });
}
showInstanceBadge();                                              // every worker start
chrome.storage.onChanged.addListener((changes, area) => {         // …and whenever the port lands
  if (area === "local" && changes.hbCdpPort) showInstanceBadge();
});

chrome.action.onClicked.addListener(() => showMonitor(true));     // toolbar button → focus it
chrome.runtime.onStartup.addListener(() => showMonitor(false));   // profile start → ensure pinned
// Closed any other way (explicit close, etc.) → reopen, unless the window itself is going away.
// Called directly (no setTimeout): chrome.tabs.* keeps the MV3 worker alive to finish; a timer wouldn't.
chrome.tabs.onRemoved.addListener((_id, info) => { if (!info.isWindowClosing) showMonitor(false); });
chrome.runtime.onInstalled.addListener(async (details) => {
  showMonitor(false);                                             // install/update → ensure pinned
  // First run on a fresh profile only: open the welcome page (not on updates/restarts).
  // Dedupe: an unpacked extension can re-fire "install" (e.g. after a Service Worker cache
  // wipe), so open hello.html only if one isn't already open — never pile up welcome tabs.
  if (details.reason === "install") {
    const url = chrome.runtime.getURL("hello.html");
    const existing = await chrome.tabs.query({ url });
    if (existing.length === 0) chrome.tabs.create({ url, active: true });
  }
});
