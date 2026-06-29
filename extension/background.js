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

// Toolbar click → open (or focus) the Agent Monitor — a CCTV grid of every
// session's live tabs. The monitor page is a normal extension page; it does its
// own CDP work as a second client on :9223.
const MONITOR_URL = chrome.runtime.getURL("monitor.html");
chrome.action.onClicked.addListener(async () => {
  const existing = await chrome.tabs.query({ url: MONITOR_URL });
  if (existing.length) {
    await chrome.tabs.update(existing[0].id, { active: true });
    await chrome.windows.update(existing[0].windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: MONITOR_URL });
  }
});

// Ship the "dedicated agent browser" feel: keep the Agent Monitor as a pinned tab at
// the front of the strip, always there. Re-asserted whenever the profile starts (or the
// extension (re)loads); query first so we never duplicate it, and just pin one that's
// already restored. active:false so it never steals the foreground on launch.
async function ensureMonitorPinned() {
  const tabs = await chrome.tabs.query({ url: MONITOR_URL });
  if (!tabs.length) {
    await chrome.tabs.create({ url: MONITOR_URL, pinned: true, active: false, index: 0 });
  } else if (!tabs[0].pinned) {
    await chrome.tabs.update(tabs[0].id, { pinned: true });
  }
}
chrome.runtime.onStartup.addListener(ensureMonitorPinned);
chrome.runtime.onInstalled.addListener(ensureMonitorPinned);

// First run on a fresh profile (reason "install" — not on updates/restarts): open the
// horse-browser welcome page so a new user knows what this browser is for.
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("hello.html"), active: true });
  }
});
