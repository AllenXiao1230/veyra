"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
async function test(file,search) {
  const registry=new Map(),documentEvents={},windowEvents={},calls=[],notices=[],made=[];
  function node(tag='div') {
    return {tag,children:[],dataset:{},style:{},value:'',textContent:'',hidden:false,open:false,attributes:{},listeners:{},
      classList:{toggle(){}},addEventListener(type,fn){this.listeners[type]=fn;},
      setAttribute(key,value){this.attributes[key]=value;},getAttribute(key){return this.attributes[key];},
      append(...items){if(this.tag==='select'&&!this.children.length&&items.length)this.value=items[0].value;this.children.push(...items);},
      prepend(){},before(){},after(){},focus(){},scrollIntoView(){},
      replaceChildren(...items){this.children=[];this.append(...items);},
      get firstElementChild(){return this.children[0];},
      get parentElement(){return this.parent||(this.parent=node());},
      querySelector(selector){if(selector==='video')return null;return this.queries?.[selector]||(this.queries||={},this.queries[selector]=node());},
      querySelectorAll(){return [];},removeAttribute(key){delete this.attributes[key];},
      showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}};
  }
  const get=id=>{if(!registry.has(id))registry.set(id,node());return registry.get(id);};
  const document={hidden:false,body:node(),getElementById:get,createElement:node,querySelector:selector=>get(selector),querySelectorAll:()=>[],addEventListener:(type,fn)=>documentEvents[type]=fn};
  document.body.dataset.page='snapshots';
  const ui={element:(tag,cls,text)=>{const value=Object.assign(node(tag),{className:cls,textContent:text||''});made.push(value);return value;},
    button:(label)=>{const value=node('button');value.label=label;made.push(value);return value;},
    pageSize:()=>get('mediaPageSize').value||'12',time:()=>'01:00:00',day:()=>'2026/10/30',dateTime:()=>'2026/10/30 01:00:00',size:()=>'3 B',icon:()=>'',backdrop(){},toast:message=>notices.push(message),
    api:(url,payload,timeout,signal)=>new Promise((resolve,reject)=>{calls.push({url,resolve,reject,payload});signal?.addEventListener('abort',()=>reject(new Error('cancelled')));})};
  const context={window:{CameraUI:ui,matchMedia:()=>({matches:true}),addEventListener:(type,fn)=>windowEvents[type]=fn},document,
    location:{search},URLSearchParams,AbortController,sessionStorage:{getItem:()=>null},setTimeout,clearTimeout,console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,file),'utf8'),context);
  const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  assert.equal(calls.length,1,file+' initial list request');
  document.hidden=true;documentEvents.visibilitychange();await settle();
  document.hidden=false;documentEvents.visibilitychange();await settle();
  assert.equal(calls.length,2,file+' retries aborted list after foreground');
  assert.equal(calls[0].url,calls[1].url,'cursor and filters preserved');
  const event={id:1,date:'2026-10-30',created_at:'2026-10-29T17:00:00Z',person_detected:false,favorite:false,snapshot_url:'/fixture.jpg',snapshot_name:'fixture.jpg',recordings:[]};
  calls[1].resolve({items:search?[event]:[],dates:[],total:search?1:0,filtered_total:0,trash_count:0,next_before:"older"});await settle();
  assert.equal(get('libraryContent').attributes['aria-busy'],'false');
  assert.equal(notices.length,0,'background cancellation is not shown as an error');
  get('loadMore').listeners.click();await settle();
  assert.equal(new URLSearchParams(calls.at(-1).url.split('?')[1]).get('before'),'older');
  get('mediaPageSize').value='48';get('mediaPageSize').listeners.change();await settle();
  const resized=new URLSearchParams(calls.at(-1).url.split('?')[1]);
  assert.equal(resized.get('limit'),'48','changed page size reaches server');
  assert.equal(resized.has('before'),false,'page size resets cursor while old request is pending');
  calls.at(-1).resolve({items:search?[event]:[],dates:[],total:search?1:0,filtered_total:0,trash_count:0,next_before:null});await settle();
  if(search) {
    made.find(n=>n.attributes['aria-label']==='查看事件快照 01:00:00').listeners.click();
    const preview=made.find(n=>n.attributes['aria-labelledby']==='eventPreviewTitle');
    const previewBody=preview.querySelector('.preview-content');
    previewBody.children[0].listeners.error();
    assert.equal(preview.open,true);
    assert.equal(previewBody.children[0].attributes.role,'alert','preview error stays in the native dialog');
    assert.equal(previewBody.children[1].textContent,'重新載入');
    previewBody.children[1].listeners.click();assert.equal(preview.open,true);
    preview.close();
    get('selectToggle').listeners.click();get('selectLoaded').listeners.click();
    made.find(n=>n.textContent==='匯出選取').listeners.click();
    const dialog=made.find(n=>n.attributes['aria-labelledby']==='eventExportTitle');
    const submitted=dialog.querySelector('form').listeners.submit({preventDefault(){}});
    calls.at(-1).reject(new Error('fixture export failure'));await submitted;
    assert.equal(dialog.open,true);
    assert.ok(made.some(n=>n.className==='dialog-error'&&!n.hidden&&n.textContent.includes('fixture export failure')));
  } else {
    get('trashToggle').listeners.click();
    assert.equal(new URLSearchParams(calls.at(-1).url.split('?')[1]).get('limit'),'48','trash uses selected page size');
    calls.at(-1).resolve({items:[{token:'a'.repeat(32),filename:'event-test.jpg',deleted_at:'2026-10-29T17:00:00Z',size:3}],total:1,next_before:null});await settle();
    get('selectToggle').listeners.click();get('selectLoaded').listeners.click();get('deleteSelected').listeners.click();
    const input=made.find(n=>n.placeholder==='輸入「永久刪除」');
    input.value='永久刪除';input.listeners.input();
    const submitted=get('confirmDelete').listeners.click();
    assert.equal(calls.at(-1).payload.action,'purge');
    calls.at(-1).reject(new Error('fixture purge failure'));await submitted;
    assert.equal(get('confirmDelete').textContent,'永久刪除');
    assert.equal(get('confirmDelete').disabled,false);
    assert.ok(made.some(n=>n.className==='dialog-error'&&!n.hidden&&n.textContent.includes('fixture purge failure')));
  }
}
(async()=>{await test('camera-timeline.js','?view=events');await test('camera-library.js','');console.log('Paged views: foreground retry and modal-local export/purge failures passed');})().catch(error=>{console.error(error);process.exitCode=1;});
