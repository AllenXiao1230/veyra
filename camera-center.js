"use strict";
(() => {
  const ui=window.CameraUI, $=id=>document.getElementById(id);
  const tools=ui.element('div','header-tools');
  const notices=ui.button('通知','bell'), work=ui.button('工作與下載','download');
  notices.id='openNotifications';work.id='openWork';
  const badge=ui.element('span','unread-badge');badge.hidden=true;badge.setAttribute('aria-hidden','true');notices.append(badge);
  $('openSettings').before(tools);tools.append(notices,work,$('openSettings'));
  const panel=ui.element('section','utility-panel');panel.id='utilityPanel';panel.hidden=true;panel.setAttribute('aria-labelledby','centerTitle');
  panel.innerHTML=`<div class="page-heading"><h2 id="centerTitle" tabindex="-1">通知</h2><button type="button" class="icon-button quiet" id="closeCenter" aria-label="關閉工作面板">${ui.icon('close')}</button></div>
    <div class="center-toolbar"><div class="view-switch" aria-label="工作面板"><button type="button" id="centerNotices">通知</button><button type="button" id="centerWork">工作與下載</button></div>
    <div id="workFilters" class="center-filters" hidden><label>種類<select id="workKind"><option value="all">全部工作</option><option value="export">事件匯出</option><option value="backup">備份工作</option><option value="download">原檔下載</option></select></label><label>狀態<select id="workState"><option value="all">全部狀態</option><option value="active">進行中</option><option value="complete">已準備完成</option><option value="failed">未完成</option><option value="expired">已過期</option></select></label></div>
    <button type="button" id="readNotices">全部標為已讀</button><button type="button" id="refreshCenter" class="icon-button" aria-label="重新整理工作面板">${ui.icon('refresh')}</button></div>
    <p id="centerScope" class="muted"></p><p id="centerError" role="alert" class="notice" hidden></p><div id="centerItems" aria-busy="false"></div>
    <div class="library-footer"><span id="centerCount" role="status"></span><div class="center-pages"><button type="button" id="centerPrevious" hidden>上一頁</button><button type="button" id="centerNext" hidden>下一頁</button></div></div>`;
  $('appHeader').after(panel);
  for(const button of [notices,work]){button.setAttribute('aria-controls','utilityPanel');button.setAttribute('aria-expanded','false');}
  let mode='notifications',lastTrigger=notices,latest=0,cursor=null,next=null,history=[],request,generation=0,timer,gone=false;
  let badgeBusy=false;
  const stateLabel={queued:'等待執行',running:'進行中',complete:'已準備完成',available:'可下載',failed:'失敗',cancelled:'已取消',interrupted:'已中斷',expired:'已過期',missing:'原檔不在媒體庫'};
  const error=message=>{$('centerError').textContent=message||'';$('centerError').hidden=!message;};
  function unread(count){badge.hidden=!count;badge.textContent=count>99?'99+':String(count);notices.setAttribute('aria-label',count?`通知，${count} 則未讀`:'通知');}
  async function updateBadge(){
    if(document.hidden||gone||badgeBusy||!panel.hidden)return;
    badgeBusy=true;
    try{const data=await ui.api('/backups-api/notifications?limit=1',undefined,7000);if(!gone&&!document.hidden)unread(data.unread);}
    catch(_){notices.setAttribute('aria-label','通知，連線暫時無法讀取');}
    finally{badgeBusy=false;}
  }
  function close(){panel.hidden=true;generation++;request?.abort();clearTimeout(timer);notices.setAttribute('aria-expanded','false');work.setAttribute('aria-expanded','false');lastTrigger.focus();updateBadge();}
  function open(name,trigger){
    mode=name;lastTrigger=trigger||(name==='work'?work:notices);cursor=null;next=null;history=[];panel.hidden=false;
    notices.setAttribute('aria-expanded',String(name==='notifications'));work.setAttribute('aria-expanded',String(name==='work'));
    $('centerTitle').textContent=name==='work'?'工作與下載':'通知';$('workFilters').hidden=name!=='work';$('readNotices').hidden=name!=='notifications';
    $('centerNotices').setAttribute('aria-pressed',String(name==='notifications'));$('centerWork').setAttribute('aria-pressed',String(name==='work'));
    $('centerScope').textContent=name==='work'?'檔案由瀏覽器下載；此處顯示伺服器準備狀態，無法確認裝置是否已存檔。':'連續兩次健康檢查異常才通知；恢復後另行記錄。';
    $('centerTitle').focus();panel.scrollIntoView({block:'start'});load();
  }
  notices.addEventListener('click',()=>!panel.hidden&&mode==='notifications'?close():open('notifications',notices));
  work.addEventListener('click',()=>!panel.hidden&&mode==='work'?close():open('work',work));
  $('closeCenter').addEventListener('click',close);$('centerNotices').addEventListener('click',()=>open('notifications'));$('centerWork').addEventListener('click',()=>open('work'));
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!panel.hidden&&!document.querySelector('dialog[open]'))close();});
  async function act(button,url,payload){
    button.disabled=true;error('');
    try{await ui.api(url,payload,15000);await load();}
    catch(problem){error(problem.message);button.disabled=false;}
  }
  function render(data){
    const host=$('centerItems');host.replaceChildren();
    if(mode==='notifications'){
      latest=data.latest;unread(data.unread);$('readNotices').disabled=!data.unread;
      if(data.active.length){const active=ui.element('p','notice',`尚未恢復：${data.active.map(i=>i.message).join('；')}`);host.append(active);}
    }
    for(const item of data.items){
      const row=ui.element('article','work-row'),copy=ui.element('div','work-copy'),actions=ui.element('div','work-actions');
      if(mode==='notifications'){
        row.dataset.state=item.kind;copy.append(ui.element('h3','',item.kind==='recovery'?'已恢復':'故障通知'));
        copy.append(ui.element('p','',item.message),ui.element('p','muted',ui.dateTime(item.at)+(item.is_read?'':' · 未讀')));
      }else{
        const title=item.kind==='export'?`事件匯出 · ${item.count||0} 件` : item.kind==='backup'?({create:'建立備份',verify:'驗證備份',recover:'隔離還原'}[item.action]||'備份工作'):item.filename;
        copy.append(ui.element('h3','',title),ui.element('p','work-state',(stateLabel[item.state]||'未知狀態')+(item.cancel_requested&&item.state==='running'?' · 取消中':'')),
          ui.element('p','muted',ui.dateTime(item.created_at)+(item.size?' · '+ui.size(item.size):'')+(item.expires_at?' · 期限 '+ui.dateTime(item.expires_at):'')));
        if(item.message&&item.state!=='complete')copy.append(ui.element('p','muted',item.message));
        if(item.error)copy.append(ui.element('p','notice',item.error));
        if(['queued','running'].includes(item.state)&&item.total_bytes>0){
          const progress=ui.element('progress');progress.max=item.total_bytes;progress.value=Math.min(item.done_bytes||0,item.total_bytes);progress.setAttribute('aria-label','工作準備進度');
          copy.append(progress,ui.element('p','muted',ui.size(item.done_bytes||0)+' / '+ui.size(item.total_bytes)));
        }
        if(item.download_url){const link=ui.element('a','button','下載');link.href=item.download_url;link.download='';link.dataset.workKnown='1';actions.append(link);}
        if(item.cancellable){const cancel=ui.element('button','quiet','取消匯出');cancel.type='button';cancel.disabled=!!item.cancel_requested;cancel.addEventListener('click',()=>act(cancel,'/backups-api/work/cancel',{id:item.id}));actions.append(cancel);}
        if(item.retryable){const retry=ui.element('button','',item.state==='expired'?'重新產生':'重試');retry.type='button';retry.addEventListener('click',()=>act(retry,'/backups-api/work/retry',{kind:item.kind,id:item.id}));actions.append(retry);}
      }
      row.append(copy,actions);host.append(row);
    }
    if(!data.items.length)host.append(ui.element('div','empty-state',mode==='notifications'?'目前沒有通知。':'沒有符合條件的工作。'));
    $('centerCount').textContent=mode==='notifications'?`${data.unread} 則未讀`:`共 ${data.total} 個工作`;
    next=data.next_before;$('centerNext').hidden=!next;$('centerPrevious').hidden=!history.length;
  }
  async function load(){
    if(panel.hidden||document.hidden||gone)return;
    clearTimeout(timer);request?.abort();request=new AbortController();const current=++generation;error('');
    $('centerItems').setAttribute('aria-busy','true');
    try{
      const params=new URLSearchParams({limit:'20'});if(cursor)params.set('before',cursor);
      if(mode==='work'){params.set('kind',$('workKind').value);params.set('state',$('workState').value);}
      const result=await ui.api('/backups-api/'+(mode==='work'?'work':'notifications')+'?'+params,undefined,12000,request.signal);
      if(current!==generation||panel.hidden||gone||document.hidden)return;render(result);
      timer=setTimeout(load,mode==='work'&&result.items.some(r=>['queued','running'].includes(r.state))?2500:15000);
    }catch(problem){if(current===generation&&!document.hidden&&!gone){error(problem.message);$('centerCount').textContent='讀取失敗，可按重新整理重試';}}
    finally{if(current===generation)$('centerItems').setAttribute('aria-busy','false');}
  }
  $('readNotices').addEventListener('click',()=>act($('readNotices'),'/backups-api/notifications/read',{through:latest}));
  $('refreshCenter').addEventListener('click',load);
  for(const id of ['workKind','workState'])$(id).addEventListener('change',()=>{cursor=null;history=[];load();});
  $('centerNext').addEventListener('click',()=>{history.push(cursor);cursor=next;load();});$('centerPrevious').addEventListener('click',()=>{cursor=history.pop();load();});
  document.addEventListener('click',event=>{
    const link=event.target.closest?.('a[download]');if(!link||link.dataset.workKnown)return;
    const url=new URL(link.href,location.href);if(url.origin!==location.origin)return;
    const match=url.pathname.match(/^\/(recordings-api\/file|ai-api\/snapshots)\/([^/]+)$/);if(!match)return;
    try{ui.api('/backups-api/work/download',{kind:match[1].startsWith('recordings')?'recording':'snapshot',filename:decodeURIComponent(match[2])},6000).catch(()=>{});}catch(_){}
  });
  const badgeTimer=setInterval(updateBadge,30000);
  document.addEventListener('visibilitychange',()=>{if(document.hidden){generation++;request?.abort();clearTimeout(timer);}else{updateBadge();load();}});
  window.addEventListener('pagehide',event=>{gone=true;generation++;request?.abort();clearTimeout(timer);if(!event.persisted)clearInterval(badgeTimer);});
  window.addEventListener('pageshow',()=>{gone=false;updateBadge();load();});
  window.CameraCenter={open:name=>open(name==='work'?'work':'notifications')};updateBadge();
})();
