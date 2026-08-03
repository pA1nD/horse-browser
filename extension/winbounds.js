// The window's real geometry, carried into the page's world.
//
// A tab that is never brought to the front reports window.outerWidth, outerHeight, screenX and
// screenY as 0 — measured on this browser: backgrounded {0,0,0,0}, the same tab foregrounded
// {4096,30,2028,1102}. A zero-size window at the screen origin is one of the oldest and most
// widely used automation signals there is, and horse-browser produced it on every page it has
// ever driven, because not stealing the operator's focus is the entire promise of the tool.
//
// So the numbers come from the browser instead of the tab: only the service worker can call
// chrome.windows.get, and only the MAIN world can define the getters, so this runs in the
// ISOLATED world between them and hands the answer over on a DOM event. Nothing here decides
// anything — realchrome.js uses these values only where the tab would otherwise report 0.
(function () {
  'use strict';
  function publish(b) {
    if (!b) return;
    try {
      document.dispatchEvent(new CustomEvent('__hb_winbounds', { detail: JSON.stringify(b) }));
    } catch (e) {}
  }
  function ask() {
    try {
      chrome.runtime.sendMessage({ hb: 'winbounds' }, function (resp) {
        void chrome.runtime.lastError;   // the SW may be asleep; silence is fine, we retry
        publish(resp);
      });
    } catch (e) {}
  }
  // The MAIN world may install its getters before this reply lands, so it asks again when it
  // is ready rather than relying on the first shot winning the race.
  document.addEventListener('__hb_winbounds_please', ask);
  ask();
  // The window can be moved or resized under a page that already read the values once.
  addEventListener('resize', ask);
})();
