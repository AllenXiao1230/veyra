"use strict";
(() => {
  if (document.body.dataset.page !== 'snapshots' || new URLSearchParams(location.search).get('view') !== 'events') return;
  const ui = window.CameraUI, $ = id => document.getElementById(id);
  let rows = [], selected = new Set(), cursor = null, history = [], next, request, generation = 0, selecting = false;
  let jobTimer, jobId, pageGone = false, loading = false, resumeList = false, jobBusy = false, jobAgain = false, jobFailures = 0;
  $('libraryTitle').textContent = '事件時間軸'; $('trashToggle').hidden = true;
  $('libraryRefresh').setAttribute('aria-label','重新整理事件');
  $('selectToggle').textContent = '批量管理'; $('selectLoaded').textContent = '全選本頁';
  $('deleteSelected').hidden = true;
  const favoriteFilter = ui.element('button', 'quiet', '只看收藏');
  favoriteFilter.type = 'button'; favoriteFilter.setAttribute('aria-pressed','false');
  const type = ui.element('select'); type.setAttribute('aria-label','事件分類');
  for(const [value,label] of [['all','全部事件'],['person','AI 標註有人'],['motion','其他動作'],['unknown','AI 未完成']]) {
    const option = ui.element('option','',label); option.value = value; type.append(option);
  }
  $('mediaDate').parentElement.append(type);
  const initialParams=new URLSearchParams(location.search);let initialDate=initialParams.get('date')||'';
  if(['person','motion','unknown'].includes(initialParams.get('type')))type.value=initialParams.get('type');
  favoriteFilter.setAttribute('aria-pressed',String(initialParams.get('favorite')==='1'));
  const advanced=ui.element('details','advanced-search');
  advanced.innerHTML='<summary>進階查找<span id="advancedSummary" class="muted"></span></summary><form id="eventSearchForm"><div class="advanced-fields"><label>開始時間（台北）<input id="eventFrom" type="datetime-local"></label><label>結束時間（含所選分鐘）<input id="eventUntil" type="datetime-local"></label><label>關鍵字<input id="eventQuery" type="search" maxlength="100" placeholder="事件編號、檔名、標籤或備註"></label><label>精確標籤<input id="eventTag" type="search" maxlength="24"></label><label>媒體<select id="eventMedia"><option value="all">全部媒體狀態</option><option value="snapshot">有快照</option><option value="video">有可播放錄影</option><option value="missing">沒有快照</option></select></label><label>最低 AI 信心（%）<input id="eventConfidence" type="number" min="0" max="100" step="1"></label><label>排序<select id="eventSort"><option value="newest">最新優先</option><option value="oldest">最舊優先</option></select></label></div><div class="search-actions"><button class="primary" type="submit">套用條件</button><button id="clearEventSearch" type="button">清除進階條件</button></div></form>';
  document.querySelector('.library-toolbar').after(advanced);
  const advancedFields={from:'eventFrom',until:'eventUntil',q:'eventQuery',tag:'eventTag',media:'eventMedia',confidence:'eventConfidence',sort:'eventSort'};
  let applied={};
  for(const [key,id] of Object.entries(advancedFields)){const value=initialParams.get(key);if(value!==null){$(id).value=value;applied[key]=value;advanced.open=true;}}
  $('eventSearchForm').addEventListener('submit',event=>{event.preventDefault();applied=Object.fromEntries(Object.entries(advancedFields).map(([key,id])=>[key,$(id).value]));load();});
  $('clearEventSearch').addEventListener('click',()=>{$('eventSearchForm').reset();applied={};load();});
  $('selectToggle').before(favoriteFilter);
  const bookmark = ui.element('button','','收藏選取'), exportSelected = ui.element('button','','匯出選取');
  bookmark.type = exportSelected.type = 'button'; $('selectionBar').append(bookmark,exportSelected);
  const previous = ui.element('button','','上一頁'); previous.type = 'button'; previous.hidden = true;
  $('loadMore').before(previous); $('loadMore').textContent = '下一頁';
  const exportStatus = ui.element('div','export-status'); exportStatus.setAttribute('role','status');
  $('libraryContent').before(exportStatus);
  const preview = ui.element('dialog','preview-dialog'); preview.setAttribute('aria-labelledby','eventPreviewTitle');
  preview.innerHTML = `<div class="dialog-heading"><h2 id="eventPreviewTitle">事件</h2><button type="button" class="icon-button quiet" aria-label="關閉事件預覽">${ui.icon('close')}</button></div><div class="preview-content"></div><div class="dialog-footer"><a class="button" download>下載原檔</a></div>`;
  document.body.append(preview); ui.backdrop(preview);
  const notes=ui.element('form','event-notes');
  notes.innerHTML='<label>標籤（以逗號分隔，最多 10 個）<input id="eventNoteTags" maxlength="250"></label><label>備註<textarea id="eventNoteText" maxlength="1000" rows="3"></textarea></label><div class="search-actions"><button type="submit">儲存標籤與備註</button><p id="eventNoteStatus" role="status" class="muted"></p></div>';
  preview.querySelector('.dialog-footer').before(notes);let editingRow=null,noteGeneration=0;
  notes.addEventListener('submit',async event=>{
    event.preventDefault();if(!editingRow)return;const row=editingRow,own=noteGeneration,save=notes.querySelector('button');save.disabled=true;
    $('eventNoteStatus').textContent='儲存中…';
    try{
      const result=await ui.api('/events-api/annotate',{id:row.id,note:$('eventNoteText').value,tags:$('eventNoteTags').value.split(/[,，]/).map(t=>t.trim()).filter(Boolean)});
      row.note=result.note;row.tags=result.tags;
      if(own===noteGeneration&&preview.open){$('eventNoteStatus').textContent='已儲存';render();}
    }catch(error){if(own===noteGeneration){$('eventNoteStatus').textContent=error.message;$('eventNoteStatus').setAttribute('role','alert');}}
    finally{if(own===noteGeneration)save.disabled=false;}
  });
  const release = () => { const video = preview.querySelector('video'); if(video) {video.pause();video.removeAttribute('src');video.load();} preview.querySelector('.preview-content').replaceChildren(); };
  preview.querySelector('button').addEventListener('click',()=>preview.close()); preview.addEventListener('close',release);
  function open(row, video) {
    editingRow=row;noteGeneration++;notes.querySelector('button').disabled=false;
    $('eventNoteTags').value=(row.tags||[]).join(', ');$('eventNoteText').value=row.note||'';
    $('eventNoteStatus').textContent='';$('eventNoteStatus').setAttribute('role','status');
    release(); $('eventPreviewTitle').textContent = ui.dateTime(row.created_at);
    const media = document.createElement(video ? 'video' : 'img');
    media.src = video ? video.playback_url : row.snapshot_url;
    if(video) {media.controls = true;media.playsInline = true;media.preload = 'metadata';}
    else media.alt = '事件快照 ' + ui.time(row.created_at);
    media.addEventListener('error',()=>{
      const error=ui.element('p','notice','無法預覽，請重試或下載原檔。');error.setAttribute('role','alert');
      const retry=ui.element('button','','重新載入');retry.type='button';retry.addEventListener('click',()=>{preview.close();open(row,video);});
      preview.querySelector('.preview-content').replaceChildren(error,retry);
      if(video){media.pause();media.removeAttribute('src');media.load();}
    },{once:true});
    preview.querySelector('.preview-content').append(media);
    preview.querySelector('a').href = video ? video.url : row.snapshot_url;
    preview.showModal(); if(video) media.play().catch(()=>{});
  }
  function selection() {
    $('selectionBar').hidden = !selecting;
    $('selectToggle').textContent = selecting ? '取消選取' : '批量管理';
    $('selectionCount').textContent = `已選取 ${selected.size} 件`;
    bookmark.disabled = exportSelected.disabled = !selected.size;
    document.querySelectorAll('.event-choice').forEach(box=>{box.hidden = !selecting;box.querySelector('input').checked=selected.has(Number(box.dataset.id));});
  }
  async function favorite(ids, enabled) {
    try {await ui.api('/events-api/favorite',{ids,favorite:enabled}); await load('same');}
    catch(error) {ui.toast(error.message);}
  }
  function render() {
    const content = $('libraryContent'); content.replaceChildren();
    let day;
    for(const row of rows) {
      if(day !== row.date) {day=row.date;content.append(ui.element('h2','day-heading',ui.day(day)));}
      const entry = ui.element('article','event-row');
      const choice = ui.element('label','event-choice'); choice.dataset.id = row.id;
      const box = document.createElement('input'); box.type='checkbox'; box.setAttribute('aria-label','選取事件 ' + ui.time(row.created_at));
      box.addEventListener('change',()=>{if(box.checked)selected.add(row.id);else selected.delete(row.id);selection();}); choice.append(box);
      const time = ui.element('time','event-time',ui.time(row.created_at)); time.dateTime=row.created_at;
      const thumb = ui.element('button','event-thumb'); thumb.type='button'; thumb.setAttribute('aria-label','查看事件快照 ' + ui.time(row.created_at));
      if(row.snapshot_url) {
        const image = document.createElement('img'); image.alt=''; image.loading='lazy';image.decoding='async';
        image.addEventListener('error',()=>{image.remove();thumb.append(ui.element('span','','查看快照'));},{once:true});
        image.src='/ai-api/thumbnail/' + encodeURIComponent(row.snapshot_name);thumb.append(image); thumb.addEventListener('click',()=>open(row));
      } else {thumb.textContent='快照已移除';thumb.disabled=true;}
      const copy = ui.element('div','event-copy'); copy.append(ui.element('strong','',row.person_detected ? '動作 · AI 標註有人' : row.ai_status==='pending' ? '動作 · AI 分析中' : row.ai_status==='unknown' ? '動作 · AI 未完成' : '偵測到動作'));
      const links = ui.element('div','event-links');
      for(const video of row.recordings) {
        const button = ui.element('button','quiet',video.complete ? '播放相關錄影' : video.state==='damaged' ? '相關錄影需修復' : video.state==='unknown' ? '相關錄影待驗證' : '相關錄影錄製中');button.type='button';button.disabled=!video.complete;
        button.addEventListener('click',()=>open(row,video));links.append(button);
      }
      if(!row.recordings.length) links.append(ui.element('span','muted','無相關錄影'));
      copy.append(links);
      if(row.tags?.length){const tags=ui.element('div','event-tags');for(const tag of row.tags)tags.append(ui.element('span','',tag));copy.append(tags);}
      if(row.note)copy.append(ui.element('p','muted',row.note.length>140?row.note.slice(0,140)+'…':row.note));
      const actions = ui.element('div','event-actions');
      const star = ui.button(row.favorite ? '取消收藏事件 ' + ui.time(row.created_at) : '收藏事件 ' + ui.time(row.created_at),'star');
      star.setAttribute('aria-pressed',String(row.favorite));star.addEventListener('click',()=>favorite([row.id],!row.favorite));
      const download = ui.button('匯出事件 ' + ui.time(row.created_at),'download');download.addEventListener('click',()=>exportDialog([row.id]));
      actions.append(star,download);entry.append(choice,time,thumb,copy,actions);content.append(entry);
    }
    if(!rows.length) content.append(ui.element('div','empty-state','沒有符合條件的事件。'));
    previous.hidden=!history.length;$('loadMore').hidden=!next;selection();
  }
  async function load(direction='reset') {
    loading=true;
    request?.abort(); request=new AbortController();const current=++generation;
    if(direction==='reset') {history=[];cursor=null;} else if(direction==='next') {history.push(cursor);cursor=next;} else if(direction==='previous') cursor=history.pop();
    selected.clear();selecting=false;$('libraryContent').setAttribute('aria-busy','true');
    if(direction==='reset'){rows=[];$('libraryContent').replaceChildren(ui.element('p','muted','正在讀取事件…'));$('libraryCount').textContent='讀取中';}
    previous.disabled=$('loadMore').disabled=true;
    try {
      const params = new URLSearchParams({limit:ui.pageSize(),type:type.value});
      if(cursor)params.set('before',cursor);if($('mediaDate').value||initialDate)params.set('date',$('mediaDate').value||initialDate);
      for(const [key,value] of Object.entries(applied))if(value&&value!=='all'&&value!=='newest')params.set(key,value);
      if(favoriteFilter.getAttribute('aria-pressed')==='true')params.set('favorite','1');
      const activeCount=Object.values(applied).filter(v=>v&&v!=='all'&&v!=='newest').length;$('advancedSummary').textContent=activeCount?' · '+activeCount+' 項條件':'';
      if(direction==='reset'&&window.history?.replaceState){const share=new URLSearchParams(params);share.delete('limit');share.delete('before');share.set('view','events');window.history.replaceState(null,'','/snapshots.html?'+share);}
      const result=await ui.api('/events-api/list?'+params,undefined,15000,request.signal);
      if(current!==generation||pageGone||document.hidden)return;rows=result.items;next=result.next_before;
      const date=$('mediaDate').value||initialDate;initialDate='';$('mediaDate').replaceChildren(ui.element('option','','全部日期'));
      $('mediaDate').firstElementChild.value='';
      for(const day of result.dates) {const option=ui.element('option','',day.date.replaceAll('-','/')+` · ${day.count} 件`);option.value=day.date;$('mediaDate').append(option);}
      if(date&&!result.dates.some(day=>day.date===date)){const option=ui.element('option','',date.replaceAll('-','/')+' · 0 件');option.value=date;$('mediaDate').append(option);}
      $('mediaDate').value=date;$('libraryCount').textContent=`${result.total} 件`;
      $('loadedCount').textContent=`本頁 ${rows.length} / 共 ${result.total} 件`;render();
      if(direction==='next'||direction==='previous')$('libraryContent').scrollIntoView({block:'start',behavior:'auto'});
    } catch(error) {
      if(current===generation&&!document.hidden&&!pageGone){
        $('libraryCount').textContent='載入失敗';
        const retry=ui.element('button','','重新載入事件');retry.type='button';retry.addEventListener('click',()=>load('same'));
        $('libraryContent').replaceChildren(ui.element('p','notice',error.message),retry);
      }
    }
    finally {if(current===generation){loading=false;$('libraryContent').setAttribute('aria-busy','false');previous.disabled=$('loadMore').disabled=false;}}
  }
  const dialog=ui.element('dialog','confirm-dialog');dialog.setAttribute('aria-labelledby','eventExportTitle');
  dialog.innerHTML='<form><div class="dialog-heading"><h2 id="eventExportTitle">匯出事件</h2></div><div class="dialog-body"><label for="eventExportFormat">格式</label><select id="eventExportFormat"><option value="zip">ZIP：紀錄與快照</option><option value="csv">CSV 紀錄</option><option value="json">JSON 紀錄</option></select><label class="export-videos"><input type="checkbox" id="eventExportVideos">包含已完成的相關影片</label><p class="muted">最多 100 件、2GiB。檔案保留 24 小時；下載會直接存入裝置。</p></div><div class="dialog-footer"><button type="button">取消</button><button type="submit" class="primary">準備匯出</button></div></form>';
  document.body.append(dialog);ui.backdrop(dialog);let exportIds=[];
  const exportError=ui.element('p','dialog-error');exportError.setAttribute('role','alert');exportError.hidden=true;dialog.querySelector('.dialog-body').append(exportError);
  dialog.querySelector('button').addEventListener('click',()=>dialog.close());
  $('eventExportFormat').addEventListener('change',()=>{$('eventExportVideos').disabled=$('eventExportFormat').value!=='zip';});
  function exportDialog(ids) {exportIds=ids;exportError.hidden=true;exportError.textContent='';$('eventExportTitle').textContent=`匯出 ${ids.length} 件事件`;dialog.showModal();}
  async function jobStatus() {
    if(!jobId||document.hidden||pageGone)return;
    if(jobBusy){jobAgain=true;return;}
    clearTimeout(jobTimer);jobBusy=true;const requestedJob=jobId;
    try {
      const {job}=await ui.api('/events-api/exports/'+jobId);
      if(requestedJob!==jobId){jobAgain=true;return;}
      if(document.hidden||pageGone)return;
      jobFailures=0;const expired=job.expires_at<=Date.now()/1000;
      exportStatus.replaceChildren(ui.element('span','',expired?'匯出已過期，請重新匯出。':job.state==='complete'?'匯出已完成，保留 24 小時。':['queued','running'].includes(job.state)?'匯出準備中，可繼續瀏覽。':job.error||'匯出未完成，請重新執行。'));
      if(job.state==='complete'&&!expired) {const link=ui.element('a','button','下載匯出檔');link.href=job.download_url;link.download='';exportStatus.append(link);}
      else if(!expired&&['queued','running'].includes(job.state))jobTimer=setTimeout(jobStatus,2000);
    } catch(error) {
      if(document.hidden||pageGone)return;
      exportStatus.textContent=error.message;
      if(++jobFailures<=3)jobTimer=setTimeout(jobStatus,Math.min(8000,1000*2**jobFailures));
      else {const retry=ui.element('button','','重新查詢匯出');retry.type='button';retry.addEventListener('click',()=>{jobFailures=0;jobStatus();});exportStatus.append(retry);}
    } finally {jobBusy=false;if(jobAgain){jobAgain=false;if(!document.hidden&&!pageGone){clearTimeout(jobTimer);jobTimer=setTimeout(jobStatus,0);}}}
  }
  dialog.querySelector('form').addEventListener('submit',async event=>{
    event.preventDefault();const button=dialog.querySelector('[type="submit"]');button.disabled=true;
    try {const result=await ui.api('/events-api/export',{ids:exportIds,format:$('eventExportFormat').value,include_videos:$('eventExportVideos').checked});jobId=result.job.id;jobFailures=0;try{sessionStorage.setItem('camera.export',jobId);}catch(_){}dialog.close();clearTimeout(jobTimer);jobStatus();}
    catch(error){exportError.hidden=false;exportError.textContent=error.message;}finally{button.disabled=false;}
  });
  $('mediaDate').addEventListener('change',()=>{initialDate='';load();});type.addEventListener('change',()=>load());
  favoriteFilter.addEventListener('click',()=>{favoriteFilter.setAttribute('aria-pressed',String(favoriteFilter.getAttribute('aria-pressed')!=='true'));load();});
  $('mediaPageSize').addEventListener('change',()=>load());
  $('libraryRefresh').addEventListener('click',()=>load());$('loadMore').addEventListener('click',()=>load('next'));previous.addEventListener('click',()=>load('previous'));
  $('selectToggle').addEventListener('click',()=>{selecting=!selecting;selected.clear();selection();});
  $('selectLoaded').addEventListener('click',()=>{rows.forEach(r=>selected.add(r.id));selection();});
  bookmark.addEventListener('click',()=>favorite([...selected],true));exportSelected.addEventListener('click',()=>exportDialog([...selected]));
  document.addEventListener('visibilitychange',()=>{
    clearTimeout(jobTimer);
    if(document.hidden){resumeList=loading;request?.abort();if(preview.open)preview.close();}
    else {if(resumeList){resumeList=false;load('same');}jobStatus();}
  });
  window.addEventListener('pagehide',()=>{pageGone=true;request?.abort();clearTimeout(jobTimer);if(preview.open)preview.close();rows=[];selected.clear();$('libraryContent').replaceChildren();});
  window.addEventListener('pageshow',event=>{pageGone=false;if(event.persisted)load();jobStatus();});
  try{jobId=sessionStorage.getItem('camera.export');}catch(_){}
  load();jobStatus();
})();
