/* Notes/alarms UI. No model, speech, certificate, or native-push integration. */
(() => {
'use strict';
const R=window.Room, E=R.esc;
const DEMO=Boolean(window.ROOM_DEMO), EMBEDDED=window.top!==window.self;
const days=['월','화','수','목','금','토','일'];
const statusNames={ringing:'울림 대기',snoozed:'5분 미룸',missed:'놓친 알람',acknowledged:'확인 완료',cancelled:'취소됨'};
let root=null, kind=null, rows={notes:[],alarms:[],events:[]}, drafts={}, query='', fetching=false, busy=false;
let sound=null, soundEnabled=false, blocked=false, displayState=null, received=-Infinity, offset=0, lastChime=0;
let soundNodes=[], pollPending=false, generation=0, screenError='', selectedEvent=null, manualTestUntil=0;
const requestKeys=new Map();
function key(){return window.crypto?.randomUUID?.()||('life-'+Date.now()+'-'+Math.random().toString(36).slice(2));}
function sameKey(name,values){const sig=JSON.stringify(values),old=requestKeys.get(name);if(old?.sig===sig)return old.id;const id=key();requestKeys.set(name,{sig,id});return id;}
async function api(path,method='GET',body){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
 try{
  const response=await fetch(path,{method,credentials:'same-origin',headers:{'X-Room-Request':'1','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
  if(!response.ok){let b={};try{b=await response.json()}catch{}const err=new Error(typeof b.detail==='string'?b.detail:Array.isArray(b.detail)?b.detail.map(x=>x.msg).join(' · '):'서버 오류 '+response.status);err.status=response.status;throw err;}
  return await response.json();
 }catch(err){if(err.name==='AbortError')throw Error(method==='GET'?'서버 응답이 지연되고 있습니다.':'저장 결과를 확인하지 못했습니다. 목록을 새로 확인한 뒤 다시 시도하세요.');throw err;}
 finally{clearTimeout(timer);}
}
function when(at,tz){if(!at)return '예약 없음';try{return new Intl.DateTimeFormat('ko-KR',{timeZone:tz||'Asia/Seoul',month:'short',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(at));}catch{return String(at);}}
function repeatLabel(a){return a.repeat==='once'?a.date+' · 한 번':a.weekdays.length===7?'매일':a.weekdays.join(',')==='0,1,2,3,4'?'평일':a.weekdays.map(x=>days[x]).join('·')+' 반복';}
function blank(type){const s=window.RoomManager?.getState();return type==='notes'?{title:'',body:'',pinned:false,shared:false}:{label:'',date:s?.today||R.isoDate(new Date(),'Asia/Seoul'),time:'07:00',timezone:s?.settings?.timezone||'Asia/Seoul',repeat:'once',weekdays:[],enabled:true};}
function feedback(message){const el=root?.querySelector('[data-life-feedback]');if(el)el.textContent=message||'';}
function shell(type){return `<div class="life-manager" data-life-manager="${type}"><div class="life-banner"><strong>${type==='notes'?'필요한 문장을, 필요한 화면에.':'시간은 서버가 기억합니다.'}</strong><p>${type==='notes'?'메모는 SQLite에 저장합니다. ‘iPad에 표시’를 켠 메모만 메모 위젯에 공유됩니다. 기존 고정 메모는 그대로 유지됩니다.':'실제 예약은 V35 서버가 처리합니다. 소리는 알람 위젯을 배치한 iPad의 열린 화면에서만 재생됩니다. 잠금·Safari 종료·오프라인·절전에서는 울림을 보장하지 않습니다.'}</p></div><div class="life-workspace"><section class="panel"><div class="panel-head"><h2>${type==='notes'?'메모 보관함':'예약한 알람'}</h2><button class="btn small" data-life-op="refresh">새로고침</button></div>${type==='notes'?'<label class="life-search">제목·내용 검색<input data-life-search type="search" placeholder="메모에서 찾기" autocomplete="off"></label>':''}<div data-life-list class="life-list" aria-live="polite">불러오는 중…</div></section><section class="panel life-editor"><div class="panel-head"><h2 data-life-editor-title>새 ${type==='notes'?'메모':'알람'}</h2><button class="btn small" data-life-op="new">새로 작성</button></div><div data-life-form></div><p class="life-error" role="status" data-life-feedback></p></section></div>${type==='alarms'?'<section class="panel life-history"><div class="panel-head"><h2>최근 알람 기록</h2><span class="muted">최대 100회 · 울림 대기 ≠ 소리 재생 확인</span></div><div data-life-history></div></section>':''}</div>`;}
function collect(){
 if(!root?.isConnected)return;const f=root.querySelector('form');if(!f)return;
 const d=new FormData(f),v=drafts[kind]||blank(kind);
 if(kind==='notes')Object.assign(v,{title:d.get('title'),body:d.get('body'),pinned:f.elements.pinned.checked,shared:f.elements.shared.checked});
 else {const preset=d.get('repeat');Object.assign(v,{label:d.get('label'),time:d.get('time'),timezone:d.get('timezone'),enabled:f.elements.enabled.checked,repeat:preset==='once'?'once':'weekly',date:preset==='once'?d.get('date'):null,weekdays:preset==='daily'?[0,1,2,3,4,5,6]:preset==='weekdays'?[0,1,2,3,4]:preset==='custom'?Array.from(f.querySelectorAll('[name=weekday]:checked')).map(e=>Number(e.value)):[]});v._preset=preset;}
 drafts[kind]=v;
}
function paintForm(){
 if(!root?.isConnected)return;const v=drafts[kind]||(drafts[kind]=blank(kind));
 root.querySelector('[data-life-editor-title]').textContent=(v.id?'수정할 ':'새 ')+(kind==='notes'?'메모':'알람');
 const base=kind==='notes'?`<label>제목<input name="title" required maxlength="120" value="${E(v.title)}" placeholder="메모 제목"></label><label>내용<textarea name="body" rows="10" maxlength="8000" placeholder="기억할 내용을 적어 주세요">${E(v.body)}</textarea></label><label class="life-check"><input type="checkbox" name="pinned" ${v.pinned?'checked':''}>위에 고정</label><label class="life-check"><input type="checkbox" name="shared" ${v.shared?'checked':''}>iPad에 표시</label><p class="field-help">표시를 끄면 관리자 보관함에만 남습니다. iPad에는 제목·내용·개수가 전달되지 않습니다.</p>`:
 (()=>{const preset=v._preset||(v.repeat==='once'?'once':v.weekdays.length===7?'daily':v.weekdays.join(',')==='0,1,2,3,4'?'weekdays':'custom');return `<label>알람 이름<input name="label" required maxlength="120" value="${E(v.label)}" placeholder="예: 아침 준비"></label><div class="life-form-row"><label>시각<input name="time" type="time" required value="${E(v.time)}"></label><label>반복<select name="repeat">${[['once','한 번'],['daily','매일'],['weekdays','평일'],['custom','요일 선택']].map(([k,n])=>`<option value="${k}" ${preset===k?'selected':''}>${n}</option>`).join('')}</select></label></div><label data-life-date class="${preset==='once'?'':'hidden'}">날짜<input name="date" type="date" value="${E(v.date||window.RoomManager?.getState()?.today||'')}" ${preset==='once'?'required':''}></label><fieldset data-life-weekdays class="life-weekdays ${preset==='custom'?'':'hidden'}"><legend>반복 요일</legend>${days.map((d,i)=>`<label><input type="checkbox" name="weekday" value="${i}" ${v.weekdays.includes(i)?'checked':''}>${d}</label>`).join('')}</fieldset><label>시간대<input name="timezone" required maxlength="80" value="${E(v.timezone)}"></label><label class="life-check"><input type="checkbox" name="enabled" ${v.enabled?'checked':''}>예약 켜기</label><p class="field-help">서버 설정이 나중에 바뀌어도 이 알람의 시간대는 유지됩니다. 저장하면 이전 울림·미루기 회차는 취소됩니다.</p>`;})();
 root.querySelector('[data-life-form]').innerHTML=`<form class="stack" data-life-editor-form>${base}<div class="form-actions"><button class="btn primary" type="submit">${v.id?'수정 저장':kind==='notes'?'메모 저장':'알람 예약'}</button>${v.id?'<button class="btn danger" type="button" data-life-op="delete">삭제</button>':''}</div></form>`;
 const f=root.querySelector('form');f.oninput=collect;f.onchange=e=>{collect();if(e.target.name==='repeat')paintForm();};
 f.onsubmit=async e=>{e.preventDefault();if(busy||!f.reportValidity())return;collect();const v=drafts[kind];
 const fields=kind==='notes'?['title','body','pinned','shared']:['label','date','time','timezone','repeat','weekdays','enabled'];
 const body=Object.fromEntries(fields.map(k=>[k,v[k]]));if(v.id)body.version=v.version;else body.request_id=sameKey(kind,body);
 busy=true;f.querySelector('[type=submit]').disabled=true;feedback('');
 try{if(DEMO)throw Error('이 화면은 예시입니다. 실제 저장은 실행 중인 서버의 관리자에서 하세요.');await api('/api/life/'+kind+(v.id?'/'+encodeURIComponent(v.id):''),v.id?'PUT':'POST',body);drafts[kind]=blank(kind);requestKeys.delete(kind);paintForm();feedback('서버에 저장했습니다.');await refresh();window.RoomManager?.refresh(false);}
 catch(err){feedback(err.message);}finally{busy=false;const b=root?.querySelector('[type=submit]');if(b)b.disabled=false;}
 };
}
function paintList(){
 if(!root?.isConnected)return;const list=root.querySelector('[data-life-list]'),scroll=list.scrollTop;
 const data=rows[kind].filter(v=>kind!=='notes'||(v.title+'\n'+v.body).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
 list.innerHTML=data.map(v=>`<button class="life-list-item ${drafts[kind]?.id===v.id?'selected':''}" data-life-op="edit" data-id="${E(v.id)}"><span class="life-item-top"><strong>${E(kind==='notes'?v.title:v.label)}</strong><span class="badge">${kind==='notes'?(v.pinned?'고정 · ':'')+(v.shared?'iPad 공유':'비공개'):(v.enabled?'예약 켜짐':'예약 꺼짐')}</span></span><span class="life-excerpt">${E(kind==='notes'?(v.body||'내용 없음').slice(0,160):v.time+' · '+repeatLabel(v))}</span><small>${E(kind==='notes'?'수정 '+when(v.updated_at):v.timezone+' · 다음 '+when(v.next_fire_at,v.timezone))}</small></button>`).join('')||'<div class="empty">'+(kind==='notes'?'표시할 메모가 없습니다.':'등록한 알람이 없습니다.')+'</div>';
 list.scrollTop=scroll;
 if(kind==='alarms'){const box=root.querySelector('[data-life-history]');box.innerHTML=rows.events.map(ev=>`<div class="life-history-row"><span><strong>${E(ev.label)}</strong><small>${E(when(ev.scheduled_at,ev.timezone))} · ${E(ev.timezone)}</small></span><span class="badge">${E(statusNames[ev.state]||ev.state)}</span>${['ringing','snoozed','missed'].includes(ev.state)?`<button class="btn small" data-life-op="event-ack" data-id="${E(ev.id)}" data-version="${ev.version}">확인·종료</button>`:''}</div>`).join('')||'<p class="muted">아직 발생한 알람이 없습니다.</p>';}
}
async function refresh(){
 if(!root?.isConnected||fetching)return;fetching=true;const target=root,type=kind;
 try{if(DEMO){const d=window.ROOM_DEMO.life_demo||{};rows[type]=d[type]||[];if(type==='alarms')rows.events=d.events||[];}
 else{const result=await api('/api/life/'+type);if(root!==target||kind!==type)return;rows[type]=result.items;if(type==='alarms')rows.events=result.events;}
 paintList();}
 catch(err){feedback(err.message);if(err.status===401)reset();}
 finally{fetching=false;}
}
function mount(el,type){
 if(!el)return;if(root===el&&kind===type)return;collect();root=el;kind=type;query='';root.innerHTML=shell(type);paintForm();refresh();
 root.querySelector('[data-life-search]')?.addEventListener('input',e=>{query=e.target.value;paintList();});
 root.onclick=async e=>{const b=e.target.closest('[data-life-op]');if(!b||busy)return;const op=b.dataset.lifeOp;
 if(op==='refresh'){refresh();return;}
 if(op==='new'){drafts[kind]=blank(kind);paintForm();paintList();feedback('');return;}
 if(op==='edit'){const item=rows[kind].find(x=>x.id===b.dataset.id);if(item){drafts[kind]=structuredClone(item);paintForm();paintList();feedback('');}return;}
 if(op==='delete'){const v=drafts[kind];if(!v?.id||!window.confirm((kind==='notes'?'메모':'알람')+'를 삭제할까요?'+(kind==='alarms'?' 울림·미루기 회차도 종료됩니다.':'')))return;
 busy=true;b.disabled=true;try{if(DEMO)throw Error('예시 화면은 실제 데이터를 변경하지 않습니다.');await api('/api/life/'+kind+'/'+encodeURIComponent(v.id),'DELETE',{version:v.version});drafts[kind]=blank(kind);paintForm();await refresh();window.RoomManager?.refresh(false);feedback('삭제했습니다.');}catch(err){feedback(err.message);}finally{busy=false;if(b.isConnected)b.disabled=false;}return;}
 if(op==='event-ack'){b.disabled=true;try{await eventAction(b.dataset.id,Number(b.dataset.version),'ack');await refresh();}catch(err){feedback(err.message);}finally{if(b.isConnected)b.disabled=false;}}
 };
}
function reset(){root=null;kind=null;rows={notes:[],alarms:[],events:[]};drafts={};requestKeys.clear();busy=false;}
function soundStatus(){return DEMO||EMBEDDED?'미리보기 · 소리 꺼짐':blocked?'연결 권한 없음':!soundEnabled?'소리 사용 안 함':sound?.state==='running'?'소리 사용 중 · 기기 음량 확인':'소리 일시정지 · 다시 허용';}
function stopTone(){manualTestUntil=0;for(const node of soundNodes){try{node.stop()}catch{}try{node.disconnect()}catch{}}soundNodes=[];}
function chime(){
 if(!soundEnabled||sound?.state!=='running'||DEMO||EMBEDDED||blocked)return;
 stopTone();for(let i=0;i<2;i++){const o=sound.createOscillator(),g=sound.createGain(),at=sound.currentTime+i*.28;o.type='sine';o.frequency.value=i?880:660;g.gain.setValueAtTime(0,at);g.gain.linearRampToValueAtTime(.16,at+.015);g.gain.linearRampToValueAtTime(0,at+.20);o.connect(g);g.connect(sound.destination);o.start(at);o.stop(at+.22);o.onended=()=>{o.disconnect();g.disconnect();};soundNodes.push(o);}
}
function displayUpdate(snapshot){
 if(blocked||!snapshot)return;const old=displayState;if(old&&snapshot.revision<old.revision)return;
 displayState=snapshot;offset=Date.parse(snapshot.as_of)-Date.now();received=performance.now();screenError='';
 if(!snapshot.alarms?.enabled)stopTone();paintSound();
}
function paintSound(){
 document.querySelectorAll('[data-life-sound-state]').forEach(e=>e.textContent=soundStatus());
 let alert=document.getElementById('lifeAlarmAlert');
 const valid=!blocked&&!document.hidden&&performance.now()-received<15000&&displayState?.alarms?.enabled;
 const now=Date.now()+offset;
 const active=valid?(displayState.alarms.events||[]).filter(e=>e.state==='ringing'&&Date.parse(e.ring_until)>now):[];
 if(!active.length){if(performance.now()>=manualTestUntil)stopTone();alert?.remove();selectedEvent=null;return;}
 if(!active.some(e=>e.id===selectedEvent))selectedEvent=active[0].id;
 const ev=active.find(e=>e.id===selectedEvent),signature=JSON.stringify([ev,active.length,soundStatus(),screenError]);
 if(!alert){alert=document.createElement('section');alert.id='lifeAlarmAlert';alert.className='life-alarm-alert';alert.setAttribute('role','region');alert.setAttribute('aria-label','발생한 알람');document.body.append(alert);}
 if(alert.dataset.signature!==signature){alert.dataset.signature=signature;alert.innerHTML=`<div><span class="eyebrow">ROOM ALARM · ${active.length}회차</span><h2>${E(ev.label)}</h2><p>${E(when(ev.due_at,ev.timezone))} · ${E(ev.timezone)}</p><p class="life-alert-state">${E(soundStatus())}</p></div><div class="life-alarm-buttons"><button class="btn" data-life-alarm="enable">소리 허용·테스트</button><button class="btn primary" data-life-alarm="ack" data-id="${E(ev.id)}" data-version="${ev.version}">확인·종료</button><button class="btn" data-life-alarm="snooze" data-id="${E(ev.id)}" data-version="${ev.version}" ${ev.snoozes>=3?'disabled':''}>5분 미루기 (${ev.snoozes}/3)</button></div>${screenError?`<p role="status">${E(screenError)}</p>`:''}`;}
 if(soundEnabled&&performance.now()-lastChime>=3500){lastChime=performance.now();chime();}
}
async function poll(){
 if(DEMO||EMBEDDED||blocked||pollPending||document.hidden||!displayState?.alarms?.enabled)return;
 pollPending=true;const epoch=generation;
 try{const s=await api('/api/life/display');if(epoch!==generation||blocked)return;displayUpdate(s);window.RoomDisplay?.updateLife(s);}
 catch(err){stopTone();if(err.status===401)stopDisplay();}
 finally{pollPending=false;}
}
async function eventAction(id,version,action){
 if(DEMO||EMBEDDED)throw Error('미리보기에서는 알람을 변경하지 않습니다.');
 return api('/api/life/events/'+encodeURIComponent(id)+'/'+action,'POST',{version,request_id:sameKey('event:'+id+':'+action,{version})});
}
function offline(){received=-Infinity;stopTone();paintSound();}
function stopDisplay(){blocked=true;generation++;displayState=null;received=-Infinity;soundEnabled=false;stopTone();paintSound();}
if(document.body.classList.contains('client-body')){
 document.addEventListener('click',async e=>{
  const b=e.target.closest('[data-life-alarm]');if(!b)return;e.stopPropagation();const op=b.dataset.lifeAlarm;
  if(b.disabled)return;b.disabled=true;
  try{
   if(op==='enable'){
    if(DEMO||EMBEDDED)throw Error('미리보기에서는 소리를 재생하지 않습니다. 실제 iPad 화면에서 허용해 주세요.');
    if(blocked)throw Error('표시 기기를 다시 연결해 주세요.');
    const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)throw Error('이 브라우저는 알람 소리 API를 지원하지 않습니다.');
    sound=sound||new Audio();await sound.resume();soundEnabled=sound.state==='running';if(!soundEnabled)throw Error('소리 허용이 필요합니다. 다시 눌러 주세요.');chime();manualTestUntil=performance.now()+700;lastChime=performance.now();R.toast('테스트 소리가 들리는지 확인하세요. 새로고침 후에는 다시 허용해야 합니다.');
   }else if(op==='mute'){soundEnabled=false;stopTone();}
   else if(op==='ack'||op==='snooze'){
    stopTone();await eventAction(b.dataset.id,Number(b.dataset.version),op);const s=await api('/api/life/display');displayUpdate(s);window.RoomDisplay?.updateLife(s);
   }
  }catch(err){screenError=err.message;R.toast(err.message);if(err.status===401)stopDisplay();else if(err.status===409)poll();}
  finally{if(b.isConnected)b.disabled=false;paintSound();}
 });
 document.addEventListener('visibilitychange',()=>{if(document.hidden){stopTone();document.getElementById('lifeAlarmAlert')?.remove();}else{received=-Infinity;poll();}});
 setInterval(paintSound,1000);setInterval(poll,5000);
}
setInterval(()=>{if(root?.isConnected)refresh();},10000);
window.RoomLife={mount,refresh,reset,displayUpdate,offline,stopDisplay,soundStatus,when,repeatLabel,api};
})();
