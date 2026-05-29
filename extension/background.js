// Browser Harness Tab Grouper — CDP-driven.
//
// The helper attaches to this service worker via CDP and calls
// self.bhGroup / self.bhList with Runtime.evaluate (awaitPromise: true).
// chrome.debugger.getTargets() bridges CDP targetIds to chrome.tabs.Tab ids.

// MV3 service workers sleep when idle (~30s). The CDP-only callers (bh_list
// etc.) can't wake us via tab events. A 30s no-op alarm is the chrome-blessed
// keepalive: each fire resets the idle timer, SW stays warm indefinitely.
chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }));
chrome.alarms.onAlarm.addListener(() => {});

const COLORS = ["blue", "cyan", "green", "yellow", "orange", "red", "pink", "purple"];

function colorForName(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return COLORS[h % COLORS.length];
}

async function tabIdForTargetId(targetId) {
  const targets = await chrome.debugger.getTargets();
  const t = targets.find(x => x.id === targetId);
  if (!t || !t.tabId) throw new Error(`no tab for CDP target ${targetId}`);
  return t.tabId;
}

self.bhGroup = async (targetId, label) => {
  const tabId = await tabIdForTargetId(targetId);
  const tab = await chrome.tabs.get(tabId);
  const existing = await chrome.tabGroups.query({ windowId: tab.windowId, title: label });
  if (existing.length) {
    await chrome.tabs.group({ tabIds: [tabId], groupId: existing[0].id });
  } else {
    const gid = await chrome.tabs.group({
      tabIds: [tabId],
      createProperties: { windowId: tab.windowId },
    });
    await chrome.tabGroups.update(gid, {
      title: label,
      color: colorForName(label),
      collapsed: false,
    });
  }
  return tabId;
};

// chrome.tabs.update({active:true}) swaps Brave's visible tab without raising
// the app — replaces CDP Target.activateTarget, which calls [NSApp activate]
// and steals macOS focus while the agent works.
self.bhActivate = async (targetId) => {
  const tabId = await tabIdForTargetId(targetId);
  await chrome.tabs.update(tabId, { active: true });
  return tabId;
};

self.bhList = async (label) => {
  const groups = await chrome.tabGroups.query({ title: label });
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
