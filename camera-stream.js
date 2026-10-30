"use strict";
(() => {
  let library,abortLibrary;
  function hlsLibrary(){
    if(window.Hls)return Promise.resolve(window.Hls);
    if(library)return library;
    library=new Promise((resolve,reject)=>{
      const script=document.createElement('script');script.src='/hls.min.js?v=1.7.2';script.async=true;
      let settled=false;
      const finish=error=>{if(settled)return;settled=true;clearTimeout(timer);script.remove();abortLibrary=null;if(error){library=null;reject(error);}else resolve(window.Hls);};
      const timer=setTimeout(()=>finish(new Error('HLS loading timed out')),12000);
      abortLibrary=()=>finish(new Error('HLS loading cancelled'));
      script.addEventListener('load',()=>finish(window.Hls?null:new Error('HLS unavailable')),{once:true});
      script.addEventListener('error',()=>finish(new Error('HLS unavailable')),{once:true});document.head.append(script);
    });return library;
  }
  class IntegratedStream {
    constructor(video,ready,failed,muted){
      this.video=video;this.ready=ready;this.failed=failed;this.muted=muted;this.generation=0;this.active=false;
      video.addEventListener('playing',()=>{if(this.active&&video.readyState>=2){this.lastFrame=Date.now();this.ready(this.protocol);}});
      video.addEventListener('error',()=>{if(this.active&&this.protocol==='hls')this.fail();});
    }
    cleanup(){
      abortLibrary?.();
      clearTimeout(this.deadline);clearInterval(this.monitor);this.request?.abort();this.pc?.close();this.pc=null;
      if(this.session){fetch(this.session,{method:'DELETE',headers:{'If-Match':'*'},keepalive:true}).catch(()=>{});this.session=null;}
      this.hls?.destroy();this.hls=null;
      if(this.frameCallback!==undefined&&this.video.cancelVideoFrameCallback)this.video.cancelVideoFrameCallback(this.frameCallback);
      this.frameCallback=undefined;this.video.pause();this.video.srcObject=null;this.video.removeAttribute('src');this.video.load();
    }
    stop(){this.active=false;this.generation++;this.cleanup();}
    setMuted(muted){this.video.muted=muted;return this.video.play();}
    play(){
      this.video.play().catch(()=>{if(this.active){this.video.muted=true;this.muted?.();this.video.play().catch(()=>this.fail());}});
    }
    start(preferHls=false){
      this.stop();this.protocol=null;this.active=true;const own=this.generation;this.video.muted=true;this.video.playsInline=true;
      this.lastFrame=Date.now();this.lastClock=-1;this.lastFrames=-1;
      if(this.video.requestVideoFrameCallback){
        const frame=()=>{if(!this.active||own!==this.generation)return;this.lastFrame=Date.now();this.frameCallback=this.video.requestVideoFrameCallback(frame);};
        this.frameCallback=this.video.requestVideoFrameCallback(frame);
      }
      this.monitor=setInterval(()=>{
        if(!this.active||document.hidden)return;
        if(!this.video.requestVideoFrameCallback){
          const frames=this.video.getVideoPlaybackQuality?.().totalVideoFrames;
          if(frames!==undefined&&frames!==this.lastFrames){this.lastFrames=frames;this.lastFrame=Date.now();}
          else if(frames===undefined&&this.video.currentTime!==this.lastClock){this.lastClock=this.video.currentTime;this.lastFrame=Date.now();}
        }
        if(Date.now()-this.lastFrame>20000)this.protocol==='webrtc'?this.hlsStart(own):this.fail();
      },3000);
      if(preferHls)this.hlsStart(own);
      else this.webrtc(own).catch(()=>{if(this.active&&own===this.generation)this.hlsStart(own);});
    }
    async webrtc(own){
      if(!window.RTCPeerConnection)throw new Error('WebRTC unavailable');
      this.protocol='webrtc';const pc=this.pc=new RTCPeerConnection({iceServers:[]});
      pc.addTransceiver('video',{direction:'recvonly'});pc.addTransceiver('audio',{direction:'recvonly'});
      pc.ontrack=event=>{if(this.active&&own===this.generation){this.video.srcObject=event.streams[0]||new MediaStream([event.track]);this.play();}};
      pc.onconnectionstatechange=()=>{if(this.active&&own===this.generation&&pc.connectionState==='failed')this.hlsStart(own);};
      this.deadline=setTimeout(()=>{if(this.active&&own===this.generation&&this.protocol==='webrtc')this.hlsStart(own);},9000);
      await pc.setLocalDescription(await pc.createOffer());
      if(pc.iceGatheringState!=='complete')await new Promise(resolve=>{
        const timeout=setTimeout(resolve,2500);pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){clearTimeout(timeout);resolve();}},{once:true});
      });
      if(!this.active||own!==this.generation||this.protocol!=='webrtc')return;
      this.request=new AbortController();
      const response=await fetch('/stream/live/whep',{method:'POST',headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp,signal:this.request.signal});
      if(!response.ok)throw new Error('WHEP failed');
      const locationHeader=response.headers.get('Location');let session=null;
      if(locationHeader){
        const locationURL=new URL(locationHeader,location.href);
        if(locationURL.origin===location.origin&&locationURL.pathname.startsWith('/stream/live/'))session=locationURL.href;
      }
      if(!this.active||own!==this.generation||this.protocol!=='webrtc'){
        if(session)fetch(session,{method:'DELETE',headers:{'If-Match':'*'},keepalive:true}).catch(()=>{});return;
      }
      this.session=session;await pc.setRemoteDescription({type:'answer',sdp:await response.text()});
      const connected=()=>{if(pc.connectionState==='connected'&&this.protocol==='webrtc')clearTimeout(this.deadline);};
      pc.addEventListener('connectionstatechange',connected);connected();
    }
    async hlsStart(own){
      if(!this.active||own!==this.generation||this.protocol==='hls')return;
      this.cleanup();this.active=true;this.protocol='hls';this.lastFrame=Date.now();
      // cleanup releases both the WebRTC decoder and its remote WHEP session.
      this.deadline=setTimeout(()=>{if(this.active&&own===this.generation&&this.video.readyState<2)this.fail();},15000);
      const url='/stream-hls/live-hls/index.m3u8';
      if(this.video.canPlayType('application/vnd.apple.mpegurl')){this.video.src=url;this.play();}
      else {
        let Hls;
        try{Hls=await hlsLibrary();}catch(_){if(this.active&&own===this.generation)this.fail();return;}
        if(!this.active||own!==this.generation)return;
        if(!Hls.isSupported()){this.fail();return;}
        this.hls=new Hls({enableWorker:true,lowLatencyMode:true,maxBufferLength:8,maxMaxBufferLength:12,
          backBufferLength:0,maxBufferSize:8*1024*1024,liveSyncDurationCount:2,liveMaxLatencyDurationCount:6,
          manifestLoadingMaxRetry:2,levelLoadingMaxRetry:2,fragLoadingMaxRetry:2});
        this.hls.on(Hls.Events.ERROR,(_,data)=>{if(data.fatal&&this.active&&own===this.generation)this.fail();});
        this.hls.attachMedia(this.video);this.hls.loadSource(url);this.play();
      }
      this.monitor=setInterval(()=>{
        if(!this.active||document.hidden)return;
        const frames=this.video.getVideoPlaybackQuality?.().totalVideoFrames;
        if(frames!==undefined&&frames!==this.lastFrames){this.lastFrames=frames;this.lastFrame=Date.now();}
        else if(frames===undefined&&this.video.currentTime!==this.lastClock){this.lastClock=this.video.currentTime;this.lastFrame=Date.now();}
        if(Date.now()-this.lastFrame>20000)this.fail();
      },3000);
    }
    fail(){if(!this.active)return;this.stop();this.failed();}
  }
  window.CameraIntegrated=IntegratedStream;
})();
