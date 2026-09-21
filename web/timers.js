/* Timer transport and opt-in foreground sound. No model or OS alarm integration. */
(() => {
'use strict';
let snapshot=null,received=0,serverTime=0,online=false,sound=null,enabled=false,previous=new Map(),polling=false;
const seen=new Set(),keys=new Map(),pending=new Set(),nodes=new Set();
const embedded=window.top!==window.self;
const key=()=>window.crypto?.randomUUID?.()||('timer-'+Date.now()+'-'+Math.random().toString(36).slice(2));
function now(){return serverTime+performance.now()-received;}
function silence(){for(const n of nodes){try{n.stop();}catch{}n.disconnect();}nodes.clear();}
function soundStatus(){return embedded?'미리보기 무음':enabled?'소리 허용됨':'소리 꺼짐';}
async function enableSound(){
 if(embedded||window.ROOM_DEMO)return;
 try{const C=window.AudioContext||window.webkitAudioContext;if(!C)throw Error('WebAudio unavailable');sound=sound||new C();await sound.resume();enabled=sound.state==='running';}
 catch{enabled=false;}
}
function mute(){enabled=false;silence();}
function chime(){
 if(!enabled||!online||embedded||document.hidden||sound?.state!=='running')return;
 for(let i=0;i<2;i++){const o=sound.createOscillator(),gain=sound.createGain(),at=sound.currentTime+i*.28;o.frequency.value=880;gain.gain.setValueAtTime(.0001,at);gain.gain.exponentialRampToValueAtTime(.13,at+.015);gain.gain.exponentialRampToValueAtTime(.0001,at+.22);o.connect(gain);gain.connect(sound.destination);nodes.add(o);o.onended=()=>{nodes.delete(o);o.disconnect();gain.disconnect();};o.start(at);o.stop(at+.24);}
}
function sync(next,connected=true){
 online=connected;
 if(!next){snapshot=null;previous.clear();silence();return;}
 const epoch=Date.parse(next.server_time);
 if(snapshot&&(next.revision<snapshot.revision||next.revision===snapshot.revision&&epoch<serverTime))return;
 const first=!snapshot;received=performance.now();serverTime=epoch;snapshot=next;
 for(const item of next.items||[]){if(!first&&item.state==='expired'&&previous.get(item.id)==='running'&&!seen.has(item.id)){seen.add(item.id);if(epoch-Date.parse(item.deadline_at)<5000)chime();}}
 previous=new Map((next.items||[]).map(t=>[t.id,t.state]));
 for(const id of seen)if(!previous.has(id))seen.delete(id);
}
async function api(path,method='GET',body){
 const ctl=new AbortController(),timeout=setTimeout(()=>ctl.abort(),10000);
 try{const r=await fetch(path,{method,credentials:'same-origin',headers:{'X-Room-Request':'1','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:ctl.signal});if(!r.ok){let data={};try{data=await r.json();}catch{}const err=Error(typeof data.detail==='string'?data.detail:'요청을 확인하지 못했습니다. 상태를 새로 확인해 주세요.');err.status=r.status;throw err;}return await r.json();}
 catch(e){if(e.name==='AbortError')throw Error('응답 지연: 목록을 확인하세요. 같은 요청을 재시도해도 중복 실행하지 않습니다.');throw e;}
 finally{clearTimeout(timeout);}
}
async function refresh(){
 if(polling||window.ROOM_DEMO||!window.RoomDisplay?.getState()?.capabilities?.timer_control)return;
 polling=true;
 try{const data=await api('/api/timers');window.RoomDisplay.updateTimers(data);}
 catch(e){if(e.status===401||e.status===403){offline();window.RoomDisplay.refresh();}}
 finally{polling=false;}
}
async function mutate(widgetId,action,body,target){
 if(!online||window.ROOM_DEMO)throw Error('서버에 연결한 뒤 타이머를 조작하세요.');
 const operation=action+':'+(action==='start'?widgetId:target),signature=JSON.stringify(body),old=keys.get(operation);
 if(pending.has(operation))throw Error('처리 중인 요청입니다.');
 const id=old?.signature===signature?old.id:key();keys.set(operation,{id,signature});pending.add(operation);
 try{const result=await api(action==='start'?'/api/timers/start':'/api/timers/'+encodeURIComponent(target)+'/stop','POST',{...body,request_id:id});keys.delete(operation);await refresh();return result;}
 catch(e){if(e.status===401||e.status===403)offline();throw e;}
 finally{pending.delete(operation);}
}
function offline(){online=false;silence();}
function clear(){offline();snapshot=null;previous.clear();seen.clear();keys.clear();pending.clear();enabled=false;}
setInterval(()=>{if(!document.hidden&&online)refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(document.hidden)silence();else refresh();});
window.addEventListener('pagehide',silence);
window.RoomTimers={now,sync,mutate,refresh,enableSound,mute,soundStatus,offline,clear};
})();
