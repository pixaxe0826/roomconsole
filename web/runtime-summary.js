/* Live-only enhancement. Existing frozen file demos do not probe a backend. */
(() => {
'use strict';
if(window.ROOM_DEMO)return;
let pending=false;
function enhance(){
 const host=document.querySelector('.llm-connection-tools');
 if(host&&!host.querySelector('[data-runtime-link]')){
  const a=document.createElement('a');a.href='/manager/runtime';a.className='btn small';
  a.dataset.runtimeLink='1';a.textContent='AI 노드 진단';host.append(a);
 }
 const engine=document.querySelector('.speech-engine');
 if(!engine||engine.dataset.runtimeSummary||pending)return;
 pending=true;const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),5000);
 fetch('/api/speech/status',{credentials:'same-origin',signal:ctrl.signal})
 .then(r=>{if(!r.ok)throw Error('status');return r.json()})
 .then(s=>{
  if(!engine.isConnected)return;
  engine.dataset.runtimeSummary='1';
  if(s.backend==='remote_http'){
   const span=engine.querySelector('span'),strong=engine.querySelector('strong');
   if(span)span.textContent='원격 whisper.cpp · 장치/스레드 미확인 · 녹음 최대 '+s.max_seconds+'초';
   if(strong)strong.textContent=s.model+' · '+s.language+' (설정값)';
  }
 }).catch(()=>{if(engine.isConnected)engine.dataset.runtimeSummary='unavailable'})
 .finally(()=>{clearTimeout(timer);pending=false;enhance()});
}
new MutationObserver(enhance).observe(document.body,{childList:true,subtree:true});
enhance();
})();
