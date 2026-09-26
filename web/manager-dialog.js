/* Runtime-only manager add-on. No implicit last-pending or voice approval. */
(() => {
'use strict';
if (window.ROOM_DEMO) return; // Standalone demos have no live dialog/source records.
const E=Room.esc, drafts=new Map(), keys=new Map(), busy=new Set();
const key=()=>globalThis.crypto?.randomUUID?.() || 'dialog-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
let queued=false, disposed=false;
async function api(path,method='GET',body){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
 try{
  const r=await fetch(path,{method,credentials:'same-origin',signal:controller.signal,
   headers:{'X-Room-Request':'1',...(body?{'Content-Type':'application/json'}:{})},
   body:body?JSON.stringify(body):undefined});
  const value=await r.json();
  if(!r.ok)throw Error(typeof value.detail==='string'?value.detail:(value.detail?.message||'대화 상태와 권한을 확인하세요.'));
  return value;
 }catch(e){if(e.name==='AbortError')throw Error('답변 접수 여부를 확인하지 못했습니다. 원래 요청의 다음 대화 링크를 확인하세요. 자동 재전송하지 않습니다.');throw e}
 finally{clearTimeout(timer)}
}
function clear(){drafts.clear();keys.clear();busy.clear()}
function root(){return document.querySelector('#llmDetail')}
function current(){return window.RoomLLM?.getSelection()}
function putDraft(id,value){if(drafts.size>=100&&!drafts.has(id))drafts.delete(drafts.keys().next().value);drafts.set(id,value)}
function render(box,d){
 const panel=document.createElement('section');panel.dataset.dialogObserved=d.id;
 const x=d.dialog;
 if(!x){panel.hidden=true;box.append(panel);return}
 panel.className='panel';panel.dataset.dialogPanel='';panel._snapshot=d;
 const pending=x.phase==='pending'&&d.status==='needs_clarification';
 const localTime=new Date(x.expires_at*1000).toLocaleTimeString('ko-KR');
 panel.innerHTML=`<h3>빠진 정보 이어서 입력</h3><p>${E(x.intent)} · ${E(x.phase)} · 유효 시각 ${E(localTime)}</p>
 <p class="field-help">선택한 요청에만 연결됩니다. 한 값씩 채우며, 기존 관리자 확인 전에는 위젯 데이터를 변경하지 않습니다.</p>
 <pre>${E(JSON.stringify(x.known_slots,null,2))}</pre>
 ${pending?`<label>${E(x.prompt)}<textarea data-dialog-input rows="2" maxlength="1500" aria-label="추가 정보 답변">${E(drafts.get(d.id)||'')}</textarea></label>
 <div class="form-actions"><button type="button" class="btn primary" data-dialog-send>추가 정보 보내기</button><button type="button" class="btn" data-dialog-cancel>입력 취소 · 변경하지 않음</button></div>`:''}
 <p data-dialog-message role="status" class="field-help">${E(x.last_error||'')}</p>
 ${x.continuation_id?`<button class="btn" data-llm="select" data-id="${E(x.continuation_id)}">다음 대화 요청 보기</button>`:''}`;
 box.insertBefore(panel,box.children[1]||null);
 // Existing delegated navigation is reused. Standalone retry of a slot-only child
 // is rejected by the backend too; hiding it here is not an authorization check.
 if(d.assistant?.dialog_state?.proof?.steps?.length||x.phase==='continued')
  box.querySelectorAll('[data-llm=retry]').forEach(b=>{b.hidden=true;b.disabled=true;b.style.display='none'});
 panel.addEventListener('input',e=>{if(e.target.matches('[data-dialog-input]'))putDraft(d.id,e.target.value)});
 panel.addEventListener('click',async e=>{
  const b=e.target.closest('[data-dialog-send],[data-dialog-cancel]');if(!b||busy.has(d.id))return;
  const text=b.hasAttribute('data-dialog-cancel')?'취소':(drafts.get(d.id)||'');
  const message=panel.querySelector('[data-dialog-message]');
  if(!text.trim()){message.textContent='요청한 값을 입력하세요.';return}
  // Only the server's clock can expire a dialog; a skewed browser clock is a display hint.
  const signature=d.id+':'+x.state_sha256+':'+text;
  const requestId=keys.get(signature)||key();keys.set(signature,requestId);busy.add(d.id);
  panel.querySelectorAll('[data-dialog-send],[data-dialog-cancel]').forEach(n=>n.disabled=true);
  try{
   const child=await api('/api/llm/requests/'+encodeURIComponent(d.id)+'/dialog/reply','POST',
    {request_id:requestId,text,expected_state_sha256:x.state_sha256});
   drafts.delete(d.id);keys.delete(signature);
   if(!panel.isConnected||current()!==d.id){Room.toast('답변이 접수됐습니다. 원래 요청의 다음 대화 링크에서 확인하세요.');return}
   const next=document.createElement('button');next.className='btn';next.dataset.llm='select';next.dataset.id=child.id;
   next.textContent='이어진 요청 보기';message.replaceChildren(next);
   next.click(); // Same visible navigation button and existing manager click handler.
  }catch(err){if(panel.isConnected)message.textContent=err.message}
  finally{busy.delete(d.id);if(panel.isConnected)panel.querySelectorAll('[data-dialog-send],[data-dialog-cancel]').forEach(n=>n.disabled=false)}
 });
}
async function update(){
 const box=root(),id=current();
 if(!box||!id){clear();return}
 if(box.querySelector(`[data-dialog-observed="${CSS.escape(id)}"]`))return;
 // Install a marker synchronously to coalesce repeated renders and our own DOM mutation.
 const marker=document.createElement('span');marker.hidden=true;marker.dataset.dialogObserved=id;box.append(marker);
 try{
  const detail=await api('/api/llm/requests/'+encodeURIComponent(id));
  if(disposed||!marker.isConnected||current()!==id)return;
  marker.remove();render(box,detail);
 }catch(err){
  if(marker.isConnected){marker.hidden=false;marker.className='field-help';marker.textContent=err.message}
 }
}
function schedule(){if(queued||disposed)return;queued=true;queueMicrotask(()=>{queued=false;update()})}
const observer=new MutationObserver(schedule);observer.observe(document.body,{childList:true,subtree:true});
document.addEventListener('click',e=>{if(e.target.closest('[data-op=logout]'))clear()},true);
window.addEventListener('pagehide',()=>{disposed=true;observer.disconnect();clear()},{once:true});
schedule();
})();
