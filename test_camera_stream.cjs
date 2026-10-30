"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  const timers=new Map();let next=0,destroyed=0,failed=0,ready=0;
  const handlers={};
  const video={muted:true,readyState:0,currentTime:0,srcObject:null,addEventListener:(k,f)=>handlers[k]=f,
    pause(){this.paused=true;},load(){},removeAttribute(k){delete this[k];},play:()=>Promise.resolve(),
    canPlayType:()=>'',getVideoPlaybackQuality:()=>({totalVideoFrames:1})};
  const instances=[];
  class Hls{
    static isSupported(){return true;}static Events={ERROR:'error'};
    constructor(config){this.config=config;instances.push(this);}on(k,f){this.error=f;}
    attachMedia(v){this.video=v;}loadSource(url){this.url=url;}destroy(){destroyed++;}
  }
  const window={Hls};const document={hidden:false};
  const context={window,document,Hls,fetch:()=>Promise.resolve(),AbortController,URL,location:{href:'https://private/',origin:'https://private'},
    setTimeout:f=>{timers.set(++next,f);return next;},setInterval:f=>{timers.set(++next,f);return next;},
    clearTimeout:id=>timers.delete(id),clearInterval:id=>timers.delete(id),console};
  vm.runInNewContext(fs.readFileSync('camera-stream.js','utf8'),context);
  const player=new window.CameraIntegrated(video,()=>ready++,()=>failed++,()=>{});
  player.start();await Promise.resolve();await Promise.resolve();
  assert.equal(instances.length,1,'missing WebRTC falls back to one HLS player');
  assert.equal(instances[0].url,'/stream-hls/live-hls/index.m3u8');
  assert.ok(instances[0].config.maxBufferSize<=8*1024*1024,'HLS bytes have a finite budget');
  video.readyState=2;handlers.playing();assert.equal(ready,1);
  await player.setMuted(false);assert.equal(video.muted,false);
  player.stop();assert.equal(player.active,false);assert.equal(destroyed,1);assert.equal(timers.size,0);
  player.start();await Promise.resolve();await Promise.resolve();
  instances[1].error(null,{fatal:true});assert.equal(failed,1);assert.equal(destroyed,2);
  assert.equal(video.srcObject,null);assert.equal(timers.size,0);
  const requests=[];
  class Peer {
    constructor(){this.iceGatheringState='complete';this.connectionState='new';this.events={};}
    addTransceiver(){}async createOffer(){return {type:'offer',sdp:'test offer'};}
    async setLocalDescription(offer){this.localDescription=offer;}
    async setRemoteDescription(answer){this.answer=answer;this.connectionState='connected';this.ontrack?.({streams:[{id:'received'}]});}
    addEventListener(type,fn){this.events[type]=fn;}close(){this.closed=true;}
  }
  window.RTCPeerConnection=Peer;context.RTCPeerConnection=Peer;
  context.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,headers:{get:()=>'/stream/live/test-session'},text:async()=>'test answer'};};
  player.start();for(let i=0;i<12;i++)await Promise.resolve();
  assert.equal(video.srcObject.id,'received');assert.equal(requests[0].options.method,'POST');
  player.stop();assert.ok(requests.some(r=>r.options.method==='DELETE'&&r.url==='https://private/stream/live/test-session'));
  assert.equal(timers.size,0);
  const posted=requests.filter(r=>r.options.method==='POST').length;
  player.start(true);for(let i=0;i<12;i++)await Promise.resolve();
  assert.equal(player.protocol,'hls');assert.equal(requests.filter(r=>r.options.method==='POST').length,posted,'explicit HLS skips ICE');
  player.stop();assert.equal(timers.size,0);
  console.log('Integrated player: WebRTC fallback, bounded HLS, mute and resource release passed');
})();
