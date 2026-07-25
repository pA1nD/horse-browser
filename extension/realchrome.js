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
(function(){ 'use strict'; try{
  var u = navigator.userAgentData; if(!u) return;
  var m = navigator.userAgent.match(/Chrome\/(\d+)/); if(!m) return; var MAJOR = m[1];
  function low(){ return [{brand:'Not;A=Brand',version:'8'},{brand:'Chromium',version:MAJOR},{brand:'Google Chrome',version:MAJOR}]; }
  // CfT's high-entropy fullVersionList is [Chromium(full), <grease>] — it has NO "…for Testing"
  // entry to rename and NO Google Chrome, so a rename-only pass leaves Google Chrome missing and
  // the grease brand inconsistent with the sync list (a tell). Rebuild it real-Chrome-shaped from
  // the Chromium FULL version — same brands as low(), but with full versions.
  function maskFull(list){
    var full = MAJOR + '.0.0.0';
    (list||[]).forEach(function(e){ if(e.brand==='Chromium' && e.version) full = e.version; });
    return [{brand:'Not;A=Brand', version:'8.0.0.0'},
            {brand:'Chromium', version:full},
            {brand:'Google Chrome', version:full}];
  }
  var proto = Object.getPrototypeOf(u);
  function nativeProxy(orig, impl){ return new Proxy(orig, { apply:function(t,ta,a){ return impl(t,ta,a); } }); }
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
  try{ if(navigator.webdriver===true) Object.defineProperty(Navigator.prototype,'webdriver',{get:function(){return false},configurable:true}); }catch(e){}
}catch(e){} })();
