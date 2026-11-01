"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  const timers=new Map();let next=0,destroyed=0,failed=0,ready=0,playCalls=0;
  const handlers={};
  const video={muted:true,readyState:0,currentTime:0,srcObject:null,addEventListener:(k,f)=>handlers[k]=f,
    pause(){this.paused=true;},load(){},removeAttribute(k){delete this[k];},play:()=>{playCalls++;return Promise.resolve();},
    canPlayType:()=>'',getVideoPlaybackQuality:()=>({totalVideoFrames:1})};
  const instances=[];
  class Hls{
    static isSupported(){return true;}static Events={ERROR:'error'};
    constructor(config){this.config=config;instances.push(this);}on(k,f){this.error=f;}
    attachMedia(v){this.video=v;}loadSource(url){this.url=url;}destroy(){destroyed++;}
  }
  const window={Hls};const document={hidden:false};
  let clock=0;
  const context={window,document,Hls,Date:{now:()=>clock},fetch:()=>Promise.resolve(),AbortController,URL,location:{href:'https://private/',origin:'https://private'},
    setTimeout:f=>{timers.set(++next,f);return next;},setInterval:f=>{timers.set(++next,f);return next;},
    clearTimeout:id=>timers.delete(id),clearInterval:id=>timers.delete(id),console};
  vm.runInNewContext(fs.readFileSync('camera-stream.js','utf8'),context);
  const player=new window.CameraIntegrated(video,()=>ready++,()=>failed++,()=>{});
  player.start();await Promise.resolve();await Promise.resolve();
  assert.equal(instances.length,1,'missing WebRTC falls back to one HLS player');
  assert.equal(instances[0].url,'/stream-hls/live-hls/index.m3u8');
  assert.equal((await player.audioStatus()).available,null,'loading metadata is not evidence of a missing audio track');
  assert.ok(instances[0].config.maxBufferSize<=8*1024*1024,'HLS bytes have a finite budget');
  video.readyState=2;handlers.playing();assert.equal(ready,1);
  await player.setMuted(false);assert.equal(video.muted,false);
  const beforeMute=playCalls;await player.setMuted(true);assert.equal(playCalls,beforeMute,'stopping listening must not start another autoplay request');
  player.stop();assert.equal(player.active,false);assert.equal(destroyed,1);assert.equal(timers.size,0);
  player.start();await Promise.resolve();await Promise.resolve();
  instances[1].error(null,{fatal:true});assert.equal(failed,1);assert.equal(destroyed,2);
  assert.equal(video.srcObject,null);assert.equal(timers.size,0);
  const requests=[];
  class MediaStream {
    constructor(){this.tracks=[];}
    getTracks(){return this.tracks;}
    getAudioTracks(){return this.tracks.filter(t=>t.kind==='audio');}
    addTrack(track){this.tracks.push(track);}
  }
  context.MediaStream=MediaStream;
  class Peer {
    constructor(){this.iceGatheringState='complete';this.connectionState='new';this.events={};}
    addTransceiver(){}async createOffer(){return {type:'offer',sdp:'test offer'};}
    async setLocalDescription(offer){this.localDescription=offer;}
    async setRemoteDescription(answer){this.answer=answer;this.connectionState='connected';
      this.ontrack?.({streams:[],track:{id:'audio',kind:'audio',readyState:'live'}});
      this.ontrack?.({streams:[],track:{id:'video',kind:'video',readyState:'live'}});
      this.ontrack?.({streams:[],track:{id:'audio',kind:'audio',readyState:'live'}});
    }
    async getStats(){return new Map([['audio',{type:'inbound-rtp',kind:'audio',bytesReceived:4000}]]);}
    addEventListener(type,fn){this.events[type]=fn;}close(){this.closed=true;}
  }
  window.RTCPeerConnection=Peer;context.RTCPeerConnection=Peer;
  context.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,headers:{get:()=>'/stream/live/test-session'},text:async()=>'test answer'};};
  player.start();for(let i=0;i<12;i++)await Promise.resolve();
  assert.equal(video.srcObject.getTracks().length,2,'streamless tracks merge without duplicates or losing audio');assert.equal(requests[0].options.method,'POST');
  assert.equal((await player.audioStatus()).receiving,true);
  assert.equal((await player.audioStatus()).receiving,false,'stalled audio is distinguished from merely present audio');
  player.stop();assert.ok(requests.some(r=>r.options.method==='DELETE'&&r.url==='https://private/stream/live/test-session'));
  assert.equal(timers.size,0);
  const posted=requests.filter(r=>r.options.method==='POST').length;
  player.start(true);for(let i=0;i<12;i++)await Promise.resolve();
  assert.equal(player.protocol,'hls');assert.equal(requests.filter(r=>r.options.method==='POST').length,posted,'explicit HLS skips ICE');
  player.stop();assert.equal(timers.size,0);
  video.getVideoPlaybackQuality=()=>({totalVideoFrames:0});
  player.start(true);for(let i=0;i<12;i++)await Promise.resolve();
  clock=25000;video.currentTime=10;timers.get(player.monitor)();assert.equal(player.active,true,'native HLS with zero frame statistics still advances by its media clock');
  const failuresBeforeStall=failed;clock=50001;timers.get(player.monitor)();assert.equal(player.active,false);assert.equal(failed,failuresBeforeStall+1,'a genuinely stalled media clock still fails');
  assert.equal(timers.size,0);
  console.log('Integrated player: WebRTC fallback, bounded HLS, mute and resource release passed');
})();
