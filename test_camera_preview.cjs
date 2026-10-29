"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function scenario() {
  let now = 100000, seed = 1, nextTimer = 0, connections = 0;
  const timers = new Map(), intervals = [], nodes = new Map();
  function element(id) {
    if (nodes.has(id)) return nodes.get(id);
    const listeners = {}, attrs = {};
    const node = {style:{}, dataset:{}, lastElementChild:{}, classList:{toggle(){}},
      naturalWidth:1280, parentElement:{dataset:{},setAttribute(){}},
      addEventListener(type, listener) { listeners[type] = listener; },
      setAttribute(k,v) { attrs[k]=v; }, getAttribute(k) { return attrs[k]; },
      removeAttribute(k) { delete attrs[k]; }, listeners};
    Object.defineProperty(node,"src",{set(v){attrs.src=v; connections++;},get(){return attrs.src;}});
    nodes.set(id,node); return node;
  }
  const document = {hidden:false, getElementById:element, addEventListener(){},
    createElement(){ return {getContext(){return {drawImage(){},getImageData(){return {data:Uint8Array.from({length:25600},()=>seed)};}};}};}};
  const ui = {preferences(){return {reconnect:true,fit:"contain"};}, recorder(){return null;},
    api:async url=> {if(url==="/camera/health") throw new Error("status timeout"); return null;}, icon(){return "";}};
  const context = {window:{CameraUI:ui,addEventListener(){}},document, Date:{now:()=>now},
    setTimeout(fn, delay){const id=++nextTimer;timers.set(id,{fn,at:now+delay});return id;},
    clearTimeout(id){timers.delete(id);},setInterval(fn){intervals.push(fn);return 1;},clearInterval(){}};
  vm.runInNewContext(fs.readFileSync(require("node:path").join(__dirname,"camera-live.js"),"utf8"), context);
  async function step(advance=true) {
    now+=3000; if(advance) seed++;
    intervals.forEach(fn=>fn());
    await Promise.resolve(); await Promise.resolve();
    for(const [id, timer] of [...timers]) if(timer.at<=now) {timers.delete(id);timer.fn();}
    await Promise.resolve(); await Promise.resolve();
  }
  // Continuous image updates with every health request failing must not reset
  // the stream, even after the old 12-second connection timeout.
  for(let i=0;i<30;i++) await step();
  assert.equal(connections,1,"healthy frames survive 90 seconds of status timeouts");
  assert.equal(element("liveStatus").lastElementChild.textContent,"即時連線");
  for(let i=0;i<10;i++) await step(false);
  assert.ok(connections>1,"a truly frozen timestamp reconnects");
  const before=connections;
  element("previewPause").listeners.click();
  for(let i=0;i<10;i++) await step(false);
  assert.equal(connections,before,"a paused preview never reconnects");
  console.log("Preview regression: progressing frames / health timeout / true stall / pause passed");
}
scenario().catch(error=>{console.error(error);process.exitCode=1;});
