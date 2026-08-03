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
      var low = function(){ return [{brand:'Not;A=Brand',version:'8'},{brand:'Chromium',version:MAJOR},{brand:'Google Chrome',version:MAJOR}]; };
      // CfT's high-entropy fullVersionList is [Chromium(full), <grease>] — it has NO "…for Testing"
      // entry to rename and NO Google Chrome, so a rename-only pass leaves Google Chrome missing and
      // the grease brand inconsistent with the sync list (a tell). Rebuild it real-Chrome-shaped from
      // the Chromium FULL version — same brands as low(), but with full versions.
      var maskFull = function(list){
        var full = MAJOR + '.0.0.0';
        (list||[]).forEach(function(e){ if(e.brand==='Chromium' && e.version) full = e.version; });
        return [{brand:'Not;A=Brand', version:'8.0.0.0'},
                {brand:'Chromium', version:full},
                {brand:'Google Chrome', version:full}];
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
    // Named rather than read from a live context: getting one at document_start would force a
    // GL init on every page load, and any string we substitute has to be internally
    // consistent anyway. x86_64 + Intel integrated is the commonest desktop pairing there is,
    // and on the hardware this targets it is the truth the browser simply cannot reach.
    var GL_VENDOR = 'Google Inc. (Intel)';
    var GL_RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6 (Core Profile) Mesa 22.3.6)';
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
})();
