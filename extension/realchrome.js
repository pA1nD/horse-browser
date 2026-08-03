// Tier 1 realness — the JS half of the Chrome-for-Testing → stable Google Chrome mask.
// Runs in the page's MAIN world at document_start (see manifest content_scripts), so it
// patches navigator.userAgentData BEFORE the page's first script reads it (verified: a
// parse-time read already sees "Google Chrome"). The wire half — the sec-ch-ua request
// header — is set to match by the service worker's dynamic declarativeNetRequest rule
// (background.js syncRealnessHeader), deriving the SAME major version from the UA.
//
// The version is DERIVED from the real browser, never hardcoded: the reduced UA carries the
// major (Chrome/151), and the full build lives in the native high-entropy values, which we
// preserve — only the brand NAME is masked ("…for Testing" → "Google Chrome"), never the
// version. So the mask can't drift when Chrome for Testing updates (a UA-vs-Client-Hints
// version mismatch is a bot tell that Cloudflare Turnstile flags).
//
// Each mask below is guarded on its OWN, deliberately. navigator.userAgentData is
// secure-context only, so a single early return on it — which is what this file used to do —
// silently disabled every later mask on any http:// page.
(function(){ 'use strict';
  function nativeProxy(orig, impl){ return new Proxy(orig, { apply:function(t,ta,a){ return impl(t,ta,a); } }); }

  // ── UA client hints ─────────────────────────────────────────────────────────────────
  try{
    var u = navigator.userAgentData;
    var m = navigator.userAgent.match(/Chrome\/(\d+)/);
    if(u && m){
      var MAJOR = m[1];
      // ADD "Google Chrome"; invent nothing else.
      //
      // The GREASE entry — the deliberately odd brand Chromium injects — is not a constant.
      // Its name, its version and its POSITION are all derived from the browser's major
      // version by Chromium's own generator, and this file used to hardcode
      // `Not;A=Brand`/`8` at the front. Measured on the browser we actually ship, Chrome 151
      // reports `Not=A?Brand`/`99` — so the mask was claiming a tuple that no Chrome 151
      // produces. A page that knows the generator could compare and see it immediately.
      //
      // So: keep every native entry exactly as it is, in the order the browser chose, and
      // insert Google Chrome beside Chromium — the one difference between Chrome for Testing
      // and stable Chrome. Nothing here can drift when Chromium changes the algorithm,
      // because nothing here reimplements it.
      var withChrome = function(list, fallbackVersion){
        var out = [], added = false;
        (list || []).forEach(function(e){
          out.push(e);
          if(!added && e && e.brand === 'Chromium'){
            out.push({brand: 'Google Chrome', version: e.version});
            added = true;
          }
        });
        if(!added) out.push({brand: 'Google Chrome', version: fallbackVersion});
        return out;
      };
      // Captured BEFORE the getter is replaced, so it is the browser's real answer.
      var NATIVE_BRANDS = (function(){ try { return u.brands.map(function(b){
        return {brand: b.brand, version: b.version}; }); } catch(e){ return []; } })();
      var low = function(){ return withChrome(NATIVE_BRANDS, MAJOR); };
      var maskFull = function(list){
        var full = MAJOR + '.0.0.0';
        (list||[]).forEach(function(e){ if(e.brand==='Chromium' && e.version) full = e.version; });
        return withChrome(list, full);
      };
      var proto = Object.getPrototypeOf(u);
      var bd = Object.getOwnPropertyDescriptor(proto,'brands');
      if(bd && bd.get && !u.brands.some(function(b){return b.brand==='Google Chrome'})){
        Object.defineProperty(proto,'brands',{ get: nativeProxy(bd.get, function(){ return low(); }), set:undefined, enumerable:bd.enumerable, configurable:true });
      }
      if(typeof proto.getHighEntropyValues==='function'){
        proto.getHighEntropyValues = nativeProxy(proto.getHighEntropyValues, function(t,ta,a){
          return Reflect.apply(t,ta,a).then(function(r){ if(r&&typeof r==='object'){ if('brands' in r) r.brands=low(); if('fullVersionList' in r) r.fullVersionList=maskFull(r.fullVersionList); } return r; });
        });
      }
      if(typeof proto.toJSON==='function'){
        proto.toJSON = nativeProxy(proto.toJSON, function(t,ta,a){ var o=Reflect.apply(t,ta,a); if(o&&typeof o==='object') o.brands=low(); return o; });
      }
    }
  }catch(e){}

  // ── navigator.webdriver ─────────────────────────────────────────────────────────────
  try{ if(navigator.webdriver===true) Object.defineProperty(Navigator.prototype,'webdriver',{get:function(){return false},configurable:true}); }catch(e){}

  // ── WebGL renderer ──────────────────────────────────────────────────────────────────
  // UNMASKED_RENDERER_WEBGL is one string that says "SwiftShader" out loud, and it is among
  // the most-checked signals there is — real desktop Chrome never reports a software
  // rasteriser. It stayed invisible on macOS for the same reason it is unavoidable on a
  // server: a Mac has hardware GL, a headless pod does not. Xvfb is a software X server, so
  // a headed browser lands on llvmpipe or SwiftShader whatever /dev/dri says — measured, not
  // assumed. And Chrome 151 needs --enable-unsafe-swiftshader for WebGL to exist at all;
  // having NO WebGL is the louder tell, so that flag is required and this mask is what makes
  // it wearable.
  //
  // Mask ONLY a software string. A real GPU's answer is left exactly alone — the truth needs
  // no help, and rewriting it would be a fresh tell of its own.
  //
  // Scope, honestly: this is the Tier-1 name mask, not a GPU spoof. getSupportedExtensions()
  // and the precision/limit parameters still describe the rasteriser underneath, so a
  // determined fingerprinter can still tell. Fixing the single loudest string is worth doing
  // on its own; claiming more would be a lie about what this file does.
  try{
    var SOFTWARE = /swiftshader|llvmpipe|softpipe|basic render|microsoft basic|lavapipe/i;
    // Chosen from the PLATFORM, because a GPU that cannot exist on the machine claimed by
    // navigator.platform is a contradiction, not a disguise. A fixed Intel-on-Mesa string
    // means an ARM Mac reporting an x86 Intel chip through a Linux graphics stack — and the
    // same fingerprinting code reads platform and renderer together. Mesa belongs to Linux,
    // D3D11 to Windows, Metal to macOS; each pairing below is one a real machine produces.
    var plat = (function(){
      try { var p = (navigator.userAgentData && navigator.userAgentData.platform) || ''; if(p) return p; } catch(e){}
      return navigator.platform || '';
    })();
    var GL_VENDOR, GL_RENDERER;
    if(/mac|darwin/i.test(plat)){
      GL_VENDOR   = 'Google Inc. (Apple)';
      GL_RENDERER = 'ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)';
    } else if(/win/i.test(plat)){
      GL_VENDOR   = 'Google Inc. (Intel)';
      GL_RENDERER = 'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)';
    } else {
      GL_VENDOR   = 'Google Inc. (Intel)';
      GL_RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6 (Core Profile) Mesa 22.3.6)';
    }
    var UNMASKED_VENDOR = 0x9245, UNMASKED_RENDERER = 0x9246;
    // Both strings are decided by ONE question — is the RENDERER software — because they are
    // read together and compared. Testing each against the pattern on its own masks the
    // renderer (it names SwiftShader) but not the vendor (it says "Google Inc. (Google)",
    // which matches nothing), and ships a browser claiming an Intel GPU from a Google vendor.
    // That pairing exists on no real machine, so a per-string test trades one tell for another.
    [self.WebGLRenderingContext, self.WebGL2RenderingContext].forEach(function(C){
      if(!C || !C.prototype || typeof C.prototype.getParameter!=='function') return;
      C.prototype.getParameter = nativeProxy(C.prototype.getParameter, function(t,ta,a){
        if(a[0]===UNMASKED_RENDERER || a[0]===UNMASKED_VENDOR){
          // t is the ORIGINAL function, so this cannot re-enter the proxy.
          var real = Reflect.apply(t, ta, [UNMASKED_RENDERER]);
          if(typeof real==='string' && SOFTWARE.test(real)){
            return a[0]===UNMASKED_RENDERER ? GL_RENDERER : GL_VENDOR;
          }
        }
        return Reflect.apply(t,ta,a);
      });
    });
  }catch(e){}

  // ── window geometry in a tab that is never brought to the front ─────────────────────
  // Measured on this browser: backgrounded, a tab reports {screenX:0, screenY:0, outerWidth:0,
  // outerHeight:0}; the same tab foregrounded reports {4096, 30, 2028, 1102}. A zero-size window
  // at the screen origin is one of the oldest automation signals in use, and this tool produced
  // it on every page it has ever driven — not taking the operator's focus is the whole promise,
  // so the tabs are always backgrounded.
  //
  // The real numbers come from the service worker via winbounds.js. Until that reply lands the
  // fallback is self-consistent rather than invented: the viewport's own size, plus the chrome
  // the browser reports around it. Every getter is guarded on the native value being 0, so a
  // foregrounded tab — which reports the truth — is never touched.
  try{
    var WB = null;
    document.addEventListener('__hb_winbounds', function(e){
      try{ WB = JSON.parse(e.detail); }catch(err){}
    });
    try{ document.dispatchEvent(new CustomEvent('__hb_winbounds_please')); }catch(e){}
    var geom = {
      screenX:     function(){ return WB ? WB.left : (screen.availLeft || 0); },
      screenY:     function(){ return WB ? WB.top  : (screen.availTop  || 0); },
      outerWidth:  function(){ return WB ? WB.width  : window.innerWidth; },
      outerHeight: function(){ return WB ? WB.height : window.innerHeight + 87; }
    };
    Object.keys(geom).forEach(function(name){
      // On the global object these are OWN accessors, not prototype properties — looking only
      // at Window.prototype found nothing and installed nothing, silently.
      var d = Object.getOwnPropertyDescriptor(window, name)
           || Object.getOwnPropertyDescriptor(Window.prototype, name);
      if(!d || !d.get) return;
      Object.defineProperty(window, name, {
        get: nativeProxy(d.get, function(t, ta, a){
          var raw = Reflect.apply(t, ta, a);
          // When the service worker has told us what the window actually is, that is the truth
          // and it wins. A backgrounded tab does not only zero these — it also reports the
          // VIEWPORT as the window, so outerHeight comes back equal to innerHeight, which says
          // the browser has no chrome around it. Passing that through because it was non-zero
          // left the second half of the tell in place.
          if (WB) return geom[name]();
          return raw ? raw : geom[name]();
        }),
        set: undefined, enumerable: d.enumerable, configurable: true
      });
    });
  }catch(e){}

  // ── screen coordinates on injected input ────────────────────────────────────────────
  // CDP's Input.dispatchMouseEvent takes ONE pair of coordinates and Chrome copies it into
  // both client and screen space. Real input does not work that way: the OS supplies screen
  // coordinates as whole numbers, and client coordinates are derived from them by subtracting
  // the window's origin and dividing by the device pixel ratio — so they come out fractional.
  //
  // Measured against a recorded human session on this machine: 276 of 276 pointer samples had a
  // fractional clientX/clientY and an integer screenX/screenY, offset from each other by the
  // window's position plus its chrome. Injected events had screenX === clientX exactly, on a
  // window sitting at (4096, 30) — and moving the window changed nothing, because Chrome never
  // consults it for these. `e.screenX === e.clientX` is one line, and no real pointer in a
  // normally-placed window satisfies it.
  //
  // So the getters reconstruct what the OS would have reported: client position plus the
  // window's own origin and chrome, rounded to whole pixels. Only when the raw value betrays an
  // injected event (screen exactly equal to client) — a genuine event is passed through
  // untouched, which also keeps this correct if the page is ever driven by a real hand.
  try{
    [['screenX','clientX'], ['screenY','clientY']].forEach(function(pair){
      var d = Object.getOwnPropertyDescriptor(MouseEvent.prototype, pair[0]);
      if(!d || !d.get) return;
      Object.defineProperty(MouseEvent.prototype, pair[0], {
        get: nativeProxy(d.get, function(t, ta, a){
          var raw = Reflect.apply(t, ta, a);
          try{
            var client = ta[pair[1]];
            if(raw !== client) return raw;              // a real event — leave it alone
            var origin = pair[0] === 'screenX'
              ? (window.screenX || 0) + Math.max(0, (window.outerWidth - window.innerWidth) / 2)
              : (window.screenY || 0) + Math.max(0, window.outerHeight - window.innerHeight);
            return Math.round(client + origin);
          }catch(e){ return raw; }
        }),
        set: undefined, enumerable: d.enumerable, configurable: true
      });
    });
  }catch(e){}
})();
