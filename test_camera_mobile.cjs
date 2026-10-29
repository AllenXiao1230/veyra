"use strict";
const assert = require('node:assert/strict');
const Frames = require('./camera-mobile.js');
const originalSet = global.setTimeout, originalClear = global.clearTimeout;
const timers = new Map(); let id=0, clock=0;
global.setTimeout=(fn,delay)=>{const key=++id;timers.set(key,{fn,at:clock+delay});return key;};
global.clearTimeout=key=>timers.delete(key);
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
async function main() {
  const live=new Set();let high=0,created=0,requests=0,concurrent=0,maxConcurrent=0;
  const URL={createObjectURL(){const key='blob:'+ ++created;live.add(key);high=Math.max(high,live.size);return key;},revokeObjectURL(key){live.delete(key);}};
  const fetch=async()=>{requests++;concurrent++;maxConcurrent=Math.max(maxConcurrent,concurrent);await Promise.resolve();concurrent--;return {ok:true,status:200,headers:{get:key=>key==='ETag'?String(requests):'image/jpeg'},blob:async()=>({size:1200})};};
  const image={src:'',decode:async()=>{},removeAttribute(){this.src='';}};
  const frames=new Frames({fetch,URL});frames.start(image,()=>{},()=>{throw new Error('unexpected frame failure');});await settle();
  for(let n=0;n<120;n++) {
    const [key,timer]=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];timers.delete(key);clock=timer.at;timer.fn();await settle();
  }
  assert.equal(maxConcurrent,1);assert.ok(high<=2);assert.equal(live.size,1);assert.ok(requests>=120);
  frames.stop();assert.equal(live.size,0);assert.equal(timers.size,0);assert.equal(image.src,'');
  let signal;
  const pending=new Frames({URL,fetch:(_,options)=>new Promise((resolve,reject)=>{signal=options.signal;signal.addEventListener('abort',()=>reject(new Error('aborted')));})});
  pending.start(image,()=>{throw new Error('late frame');},()=>{throw new Error('cancel must not report failure');});
  pending.stop();await settle();assert.ok(signal.aborted);assert.equal(timers.size,0);assert.equal(live.size,0);
  const failed=new Frames({URL,fetch:async()=>({ok:false,status:503,headers:{get:()=>null}})});
  failed.start(image,()=>{},()=>{},()=>false);await settle();assert.equal(timers.size,0);
  console.log('Mobile lifecycle: 120 frames, one in-flight request, <=2 URLs, cancellation and cleanup passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{global.setTimeout=originalSet;global.clearTimeout=originalClear;});
