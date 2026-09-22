/* Instance-scoped timer transport. SQLite deadlines are authoritative. */
(() => {
'use strict';
let snapshot=null,received=0,receivedWall=0,serverTime=0,online=false,sound=null,previous=new Map(),poll=null,generation=0,resuming=false;
const enabled=new Set(),seen=new Set(),keys=new Map(),pending=new Set(),nodes=new Map();
const embedded=window.top!==window.self;
const key=()=>window.crypto?.randomUUID?.()||('timer-'+Date.now()+'-'+Math.random().toString(36).slice(2));
function now(){return snapshot?serverTime+performance.now()-received:Date.now();}
function needsSync(){
 // Do not substitute a changed client wall clock for the authoritative server.
 // A suspended monotonic clock or manual wall-clock change needs a fresh sample.
 const drift=snapshot&&Math.abs((Date.now()-receivedWall)-(performance.now()-received))>2000;
 if(drift&&!resuming){resuming=true;refresh(true);}
 return resuming;
}
function silence(widgetId){for(const [n,owner] of nodes){if(widgetId!==undefined&&owner!==widgetId)continue;try{n.stop();}catch{}n.disconnect();nodes.delete(n);}}
function allowed(widgetId){return enabled.has('*')||enabled.has(widgetId);}
function soundStatus(widgetId){return embedded?'미리보기 무음':(widgetId===undefined?enabled.size>0:allowed(widgetId))?'소리 허용됨':'소리 꺼짐';}
async function enableSound(widgetId='*'){
 if(embedded||window.ROOM_DEMO)return;
 try{const C=window.AudioContext||window.webkitAudioContext;if(!C)throw Error('WebAudio unavailable');sound=sound||new C();await sound.resume();if(sound.state==='running')enabled.add(widgetId);}
 catch{enabled.delete(widgetId);}
}
function mute(widgetId){if(widgetId===undefined)enabled.clear();else enabled.delete(widgetId);silence(widgetId);}
function chime(item){
 if(!allowed(item.widget_id)||!online||embedded||document.hidden||sound?.state!=='running')return;
 for(let i=0;i<2;i++){const o=sound.createOscillator(),gain=sound.createGain(),at=sound.currentTime+i*.28;o.frequency.value=880;gain.gain.setValueAtTime(.0001,at);gain.gain.exponentialRampToValueAtTime(.13,at+.015);gain.gain.exponentialRampToValueAtTime(.0001,at+.22);o.connect(gain);gain.connect(sound.destination);nodes.set(o,item.widget_id);o.onended=()=>{nodes.delete(o);o.disconnect();gain.disconnect();};o.start(at);o.stop(at+.24);}
}
function sync(next,connected=true){
 online=connected;
 if(!next){snapshot=null;previous.clear();silence();return;}
 const epoch=Date.parse(next.server_time);
 if(!Number.isFinite(epoch))return;
 // Reusing a cached /api/state snapshot on reconnect must NOT restart its clock.
 if(snapshot&&(next.revision<snapshot.revision||next.revision===snapshot.revision&&epoch<=serverTime))return;
 const first=!snapshot;received=performance.now();receivedWall=Date.now();serverTime=epoch;snapshot=next;resuming=false;
 for(const item of next.items||[]){if(!first&&item.state==='expired'&&previous.get(item.id)==='running'&&!seen.has(item.id)){seen.add(item.id);if(epoch-Date.parse(item.deadline_at)<5000)chime(item);}}
 previous=new Map((next.items||[]).map(t=>[t.id,t.state]));
 for(const id of seen)if(!previous.has(id))seen.delete(id);
}
async function api(path,method='GET',body){
 const ctl=new AbortController(),timeout=setTimeout(()=>ctl.abort(),10000);
 try{const r=await fetch(path,{method,credentials:'same-origin',cache:'no-store',headers:{'X-Room-Request':'1','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:ctl.signal});if(!r.ok){let data={};try{data=await r.json();}catch{}const err=Error(typeof data.detail==='string'?data.detail:'요청을 확인하지 못했습니다. 상태를 새로 확인해 주세요.');err.status=r.status;throw err;}return await r.json();}
 catch(e){if(e.name==='AbortError')throw Error('응답 지연: 상태를 확인하세요. 같은 요청의 재시도는 중복 실행하지 않습니다.');throw e;}
 finally{clearTimeout(timeout);}
}
function refresh(force=false){
 if(poll)return force?poll.then(()=>refresh()):poll;
 if(window.ROOM_DEMO||!window.RoomDisplay?.getState()?.capabilities?.timer_control)return Promise.resolve();
 const token=generation;
 poll=(async()=>{
  try{const data=await api('/api/timers');if(token===generation)window.RoomDisplay.updateTimers(data);}
  catch(e){if(token===generation&&(e.status===401||e.status===403)){offline();window.RoomDisplay.refresh();}}
  finally{if(token===generation)poll=null;}
 })();return poll;
}
async function mutate(widgetId,action,body,target){
 if(!online||window.ROOM_DEMO)throw Error('서버에 연결한 뒤 타이머를 조작하세요.');
 if(action==='start'&&!widgetId)throw Error('타이머 위젯을 선택하세요.');
 const operation=widgetId||'recovery:'+target,payload={...body,...(widgetId?{widget_id:widgetId}:{})};
 const signature=JSON.stringify([action,target||null,payload]),old=keys.get(operation),token=generation;
 if(pending.has(operation))throw Error('이 위젯은 요청 처리 중입니다.');
 const id=old?.signature===signature?old.id:key();keys.set(operation,{id,signature});pending.add(operation);
 try{
  const result=await api(action==='start'?'/api/timers/start':'/api/timers/'+encodeURIComponent(target)+'/stop','POST',{...payload,request_id:id});
  if(token!==generation)throw Error('연결이 변경되었습니다. 화면을 다시 확인하세요.');
  keys.delete(operation);
  // The mutation response wins immediately, even while an older poll is running.
  if(result.snapshot)window.RoomDisplay.updateTimers(result.snapshot);else await refresh();
  return result;
 }catch(e){if(token===generation){if(e.status>=400&&e.status<500&&e.status!==408)keys.delete(operation);if(e.status===401||e.status===403)offline();}throw e;}
 finally{if(token===generation)pending.delete(operation);}
}
function offline(){online=false;silence();}
function clear(){generation++;offline();snapshot=null;previous.clear();seen.clear();keys.clear();pending.clear();enabled.clear();poll=null;resuming=false;}
setInterval(()=>{if(!document.hidden&&online)refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(document.hidden)silence();else{resuming=true;refresh(true);}});
window.addEventListener('pageshow',e=>{if(e.persisted){resuming=true;refresh(true);}});
window.addEventListener('pagehide',()=>silence());
window.RoomTimers={now,needsSync,sync,mutate,refresh,enableSound,mute,soundStatus,offline,clear};
})();
