// Tier 1 realness — the JS half of the Chrome-for-Testing → stable Google Chrome mask.
// Runs in the page's MAIN world at document_start (see manifest content_scripts), so it
// patches navigator.userAgentData BEFORE the page's first script reads it (verified: a
// parse-time read already sees "Google Chrome", 10/10). The wire half — the sec-ch-ua
// request header — is done coherently by declarativeNetRequest (rules.json), so JS and
// headers agree. Payload lifted verbatim from the proven real_chrome() helper.
(function(){ 'use strict'; try{
  var u = navigator.userAgentData; if(!u) return;
  var FULL='150.0.7871.47', MAJOR='150';
  function low(){ return [{brand:'Not;A=Brand',version:'8'},{brand:'Chromium',version:MAJOR},{brand:'Google Chrome',version:MAJOR}]; }
  function full(){ return [{brand:'Not;A=Brand',version:'8.0.0.0'},{brand:'Chromium',version:FULL},{brand:'Google Chrome',version:FULL}]; }
  var proto = Object.getPrototypeOf(u);
  function nativeProxy(orig, impl){ return new Proxy(orig, { apply:function(t,ta,a){ return impl(t,ta,a); } }); }
  var bd = Object.getOwnPropertyDescriptor(proto,'brands');
  if(bd && bd.get && !u.brands.some(function(b){return b.brand==='Google Chrome'})){
    Object.defineProperty(proto,'brands',{ get: nativeProxy(bd.get, function(){ return low(); }), set:undefined, enumerable:bd.enumerable, configurable:true });
  }
  if(typeof proto.getHighEntropyValues==='function'){
    proto.getHighEntropyValues = nativeProxy(proto.getHighEntropyValues, function(t,ta,a){
      return Reflect.apply(t,ta,a).then(function(r){ if(r&&typeof r==='object'){ if('brands' in r) r.brands=low(); if('fullVersionList' in r) r.fullVersionList=full(); if('uaFullVersion' in r) r.uaFullVersion=FULL; } return r; });
    });
  }
  if(typeof proto.toJSON==='function'){
    proto.toJSON = nativeProxy(proto.toJSON, function(t,ta,a){ var o=Reflect.apply(t,ta,a); if(o&&typeof o==='object') o.brands=low(); return o; });
  }
  try{ if(navigator.webdriver===true) Object.defineProperty(Navigator.prototype,'webdriver',{get:function(){return false},configurable:true}); }catch(e){}
}catch(e){} })();
