// "Open the Monitor →" — focus the pinned Monitor tab if it's there, else open it pinned.
// External file (not inline): MV3 extension pages block inline <script> under the default CSP.
document.getElementById("open-monitor").addEventListener("click", async () => {
  const url = chrome.runtime.getURL("monitor.html");
  const tabs = await chrome.tabs.query({ url });
  if (tabs.length) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url, pinned: true, index: 0 });
  }
});
