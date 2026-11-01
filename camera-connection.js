"use strict";
(() => {
  class ConnectionWindow {
    constructor(){this.samples=[];this.reconnects=0;this.failed=false;}
    add(ok,latency,now=Date.now()){
      if(ok&&this.failed)this.reconnects++;
      this.failed=!ok;this.samples.push({ok,latency,at:now});
      this.samples=this.samples.filter(s=>now-s.at<=300000).slice(-60);
      const good=this.samples.filter(s=>s.ok),times=good.map(s=>s.latency).sort((a,b)=>a-b);
      const median=times.length?Math.round(times[Math.floor(times.length/2)]):null;
      return {ok,total:this.samples.length,good:good.length,latency:median,reconnects:this.reconnects,
        label:!ok?'連線中斷':this.samples.length<6?'穩定度觀察中':good.length<this.samples.length?'連線不穩定':median>1000?'回應較慢':'連線穩定'};
    }
  }
  window.CameraConnectionWindow=ConnectionWindow;
  const status=document.getElementById('serverConnection'),metrics=document.getElementById('connectionMetrics');
  if(!status)return;
  const history=new ConnectionWindow();let busy=false,gone=false,request;
  async function sample(){
    if(busy||document.hidden||gone)return;busy=true;const start=performance.now();request=new AbortController();
    let ok=false;
    try{await window.CameraUI.api('/ai-api/status',undefined,5000,request.signal);ok=true;}catch(_){}
    finally{busy=false;}
    if(document.hidden||gone)return;
    const state=history.add(ok,performance.now()-start);status.dataset.state=ok?'ok':'error';
    status.lastElementChild.textContent=ok?'伺服器已連線 · '+state.label:'伺服器連線中斷';
    metrics.textContent=`近 5 分鐘觀測 ${state.good}/${state.total} 次成功 · 回應 ${state.latency===null?'—':state.latency+' ms'} · 本頁重連 ${state.reconnects} 次`;
  }
  const timer=setInterval(sample,5000);sample();
  document.addEventListener('visibilitychange',()=>{if(document.hidden)request?.abort();else sample();});
  window.addEventListener('pagehide',event=>{gone=true;request?.abort();if(!event.persisted)clearInterval(timer);});
  window.addEventListener('pageshow',()=>{gone=false;sample();});
  window.addEventListener('online',sample);window.addEventListener('offline',sample);
})();
