(() => {
'use strict';
const R=Room,$=s=>document.querySelector(s),grid=$('#grid'),focus=$('#focus'),focusContent=$('#focusContent');
const demo=!!window.ROOM_DEMO,commands=new Set(),pendingTasks=new Set(),demoRequests=new Map();
let state,selectedDate,month,expanded=null,offset=0,socket,renderId=0,cleanups=[],modules={},followToday=true,stopped=false,loading=false,online=false,refreshQueued=false;
function notice(text){$('#notice').textContent=text||'';$('#notice').classList.toggle('hidden',!text)}
function connection(ok,label){
 const wasOnline=online;online=ok;
 $('#connection').innerHTML=`<i class="status-dot ${ok?'':'off'}"></i> ${R.esc(label||(ok?'서버 연결됨':'다시 연결 중'))}`;
 if(!ok&&state&&!demo)notice('실시간 연결을 재시도하고 있습니다. 마지막으로 받은 내용을 표시합니다.');else if(ok)notice('');
 if(state&&!demo&&!stopped&&wasOnline!==ok)render();
}
async function loadWidgets(){
 for(const m of state.widgets){
  const key=m.id+'@'+m.version;
  if(modules[key])continue;
  try{
   modules[key]=demo?window.ROOM_WIDGETS[m.id]:await import(`${m.baseUrl}/${m.entry}?v=${encodeURIComponent(m.version)}`);
   if(!demo){
    let css=document.querySelector(`link[data-widget="${m.id}"]`);
    if(!css){css=document.createElement('link');css.rel='stylesheet';css.dataset.widget=m.id;document.head.append(css)}
    css.href=`${m.baseUrl}/${m.style}?v=${encodeURIComponent(m.version)}`;
   }
  }catch(e){console.error('Widget failed:',m.id,e);modules[key]={render:()=>'<div class="empty">위젯을 불러오지 못했습니다.</div>'}}
 }
}
function now(){return new Date(Date.now()+offset)}
function canCompleteTasks(){return !stopped&&(demo||online&&state?.capabilities?.task_completion===true)}
function context(instance,ex=false,compact=false){
 return {state,instance,selectedDate,month,expanded:ex,compact,now:now(),util:R,selectDate,changeMonth,expand:()=>openWidget(instance.id),openWidget,
  canCompleteTasks:canCompleteTasks(),setTaskCompletion,taskPending:id=>pendingTasks.has(id)};
}
function moduleFor(instance){const m=state.widgets.find(m=>m.id===instance.type);return m?modules[m.id+'@'+m.version]:null}
function widgetNode(instance,ex=false){
 const el=document.createElement('section');el.className='widget';el.dataset.widgetId=instance.id;el.setAttribute('aria-label',instance.title||instance.type);
 if(!ex){el.style.gridColumn=`${instance.x+1} / span ${instance.w}`;el.style.gridRow=`${instance.y+1} / span ${instance.h}`;el.tabIndex=0;el.setAttribute('role','button')}
 const rect=grid.getBoundingClientRect(),w=(rect.width-(state.layout.columns-1)*16)/state.layout.columns*instance.w,h=(rect.height-(state.layout.rows-1)*16)/state.layout.rows*instance.h;
 const compact=!ex&&(w<245||h<155||(instance.type==='calendar'&&(w<300||h<255))||(instance.type==='todos'&&h<255));
 if(!ex&&(w<95||h<72))el.classList.add('micro');if(!ex&&h<45)el.classList.add('nano');
 const ctx=context(instance,ex,compact),mod=moduleFor(instance);
 try{el.innerHTML=mod?.render?mod.render(ctx):'<div class="empty">위젯 폴더를 확인하세요.</div>';const cleanup=mod?.bind?.(el,ctx);if(typeof cleanup==='function')cleanups.push(cleanup)}
 catch(err){console.error(err);el.innerHTML='<div class="empty">위젯 표시 오류 · 관리자에서 확인해 주세요.</div>'}
 if(!ex){
  el.addEventListener('click',e=>{if(e.target.closest('[data-stop]'))return;openWidget(instance.id)});
  el.addEventListener('keydown',e=>{if(e.target===el&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openWidget(instance.id)}});
 }
 return el;
}
function scrollKey(el){return (el.closest('#focus')?'focus:':'grid:')+el.closest('[data-widget-id]').dataset.widgetId}
async function render({preserveScroll=true}={}){
 if(!state||stopped)return;
 const generation=++renderId;await loadWidgets();if(generation!==renderId||stopped)return;
 const scrolls=new Map();
 if(preserveScroll)document.querySelectorAll('.widget .todo-list').forEach(el=>scrolls.set(scrollKey(el),el.scrollTop));
 const active=document.activeElement,activeId=active?.dataset.completeTask,activeArea=active?.closest('#focus')?'#focus':'#grid';
 cleanups.forEach(fn=>fn());cleanups=[];
 document.body.classList.toggle('dark',state.settings.theme==='dark');$('#hubTitle').textContent=state.settings.title;
 grid.style.gridTemplateColumns=`repeat(${state.layout.columns},minmax(0,1fr))`;grid.style.gridTemplateRows=`repeat(${state.layout.rows},minmax(0,1fr))`;
 grid.replaceChildren(...state.layout.widgets.map(w=>widgetNode(w)));
 if(expanded&&!state.layout.widgets.some(w=>w.id===expanded))expanded=null;
 focus.classList.toggle('hidden',!expanded);$('#display').classList.toggle('hidden',!!expanded);focusContent.replaceChildren();
 if(expanded){const instance=state.layout.widgets.find(w=>w.id===expanded);$('#focusTitle').textContent=instance.title||state.widgets.find(w=>w.id===instance.type)?.name||instance.type;focusContent.append(widgetNode(instance,true))}
 if(preserveScroll)document.querySelectorAll('.widget .todo-list').forEach(el=>{el.scrollTop=scrolls.get(scrollKey(el))||0});
 if(activeId){const el=[...document.querySelectorAll(activeArea+' [data-complete-task]')].find(b=>b.dataset.completeTask===activeId);if(el&&!el.disabled)el.focus({preventScroll:true})}
 presence();
}
function presence(){socket?.send({type:'presence',view:expanded||'home',viewport:`${innerWidth}x${innerHeight}`})}
function selectDate(date){
 if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!Number.isFinite(R.dayDate(date).getTime()))return;
 selectedDate=date;month=date.slice(0,7);followToday=date===state.today;render({preserveScroll:false});
}
function changeMonth(value){
 if(value==='today'){selectedDate=state.today;month=state.today.slice(0,7);followToday=true}
 else{const d=R.dayDate(month+'-01');d.setUTCMonth(d.getUTCMonth()+Number(value));month=d.toISOString().slice(0,7)}
 render();
}
function openWidget(id){if(!state.layout.widgets.some(w=>w.id===id))return;expanded=id;render()}
function home(){expanded=null;$('#display').classList.remove('hidden');focus.classList.add('hidden');render()}
function applyState(next){
 // An old in-flight GET must not undo an already acknowledged completion.
 if(state&&next.revision<state.revision)return;
 if(state){const local=new Map(state.tasks.map(t=>[t.id,t]));next.tasks=next.tasks.map(t=>{const current=local.get(t.id);return current&&current.version>t.version?current:t})}
 state=next;offset=new Date(next.server_time).getTime()-Date.now();
 if(!selectedDate){selectedDate=next.today;month=next.today.slice(0,7)}
 render();
}
function requirePairing(message='기기 연결 필요'){
 stopped=true;++renderId;socket?.close();pendingTasks.clear();cleanups.forEach(fn=>fn());cleanups=[];
 expanded=null;focus.classList.add('hidden');focusContent.replaceChildren();$('#display').classList.remove('hidden');
 grid.innerHTML=`<div class="loading-card">${R.icon('screen',40)}<h2>이 화면을 방과 연결해 주세요</h2><p>관리자 → 표시 기기에서 연결 링크를 만든 뒤<br>이 iPad의 Safari로 열어 주세요.<br>키보드 입력 없이 연결됩니다.</p></div>`;
 connection(false,message);notice('');
}
async function refresh(){
 if(stopped)return;if(loading){refreshQueued=true;return}
 loading=true;
 try{const next=await R.api('/api/state');if(!stopped){applyState(next);if(!socket||!online)connection(true,'서버 데이터 수신됨')}}
 catch(e){if(e.status===401)requirePairing();else connection(false,'오프라인 · 마지막 화면')}
 finally{loading=false;if(refreshQueued&&!stopped){refreshQueued=false;queueMicrotask(refresh)}}
}
async function completionRequest(id,body){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
 try{
  const response=await fetch('/api/tasks/'+encodeURIComponent(id)+'/completion',{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Room-Request':'1'},body:JSON.stringify(body),signal:controller.signal});
  if(!response.ok){let data={};try{data=await response.json()}catch{}const e=new Error(typeof data.detail==='string'?data.detail:`서버 오류 ${response.status}`);e.status=response.status;throw e}
  return await response.json();
 }finally{clearTimeout(timer)}
}
function demoCompletion(id,body){
 if(!window.ROOM_EMBEDDED_DEMO){
  const t=state.tasks.find(t=>t.id===id);if(!t)throw Error('작업이 없습니다.');
  if(t.version!==body.version){const e=new Error('최신 상태를 확인해 주세요.');e.status=409;throw e}
  state.revision++;return Promise.resolve({...t,completed:body.completed,version:t.version+1,updated_at:now().toISOString()});
 }
 return new Promise((resolve,reject)=>{
  const requestId='complete-'+Date.now()+'-'+Math.random().toString(36).slice(2);
  const timer=setTimeout(()=>{demoRequests.delete(requestId);reject(Error('미리보기 관리자 응답이 없습니다.'))},5000);
  demoRequests.set(requestId,{resolve,reject,timer});
  parent.postMessage({type:'room-demo-completion',requestId,id,...body},'*');
 });
}
async function setTaskCompletion(id,completed){
 if(typeof completed!=='boolean'||pendingTasks.has(id)||!state)return;
 if(!canCompleteTasks()){R.toast('서버 연결 후 완료 상태를 변경할 수 있어요.');return}
 const t=state.tasks.find(t=>t.id===id);if(!t)return;if(t.completed===completed)return;
 pendingTasks.add(id);render();
 try{
  const body={completed,version:t.version};
  const saved=await (demo?demoCompletion(id,body):completionRequest(id,body));
  if(stopped)return;
  state.tasks=state.tasks.map(current=>current.id===id&&current.version<=saved.version?saved:current);
  R.toast(completed?'완료했어요.':'완료를 취소했어요.');
 }catch(e){
  if(e.status===401){requirePairing();R.toast('연결 권한이 만료되었습니다. 관리자에서 다시 연결해 주세요.')}
  else if(e.status===409)R.toast('다른 화면에서 변경된 할 일이에요. 최신 상태를 확인한 뒤 다시 눌러 주세요.');
  else if(e.status===404)R.toast('삭제된 할 일이에요. 목록을 다시 불러옵니다.');
  else if(e.status===405)R.toast('서버 업데이트와 재시작이 필요합니다.');
  else R.toast('저장 결과를 확인하지 못했어요. 서버 연결 후 최신 상태를 확인해 주세요.');
 }finally{
  pendingTasks.delete(id);if(!stopped){await render();if(!demo)refresh()}
 }
}
async function remote(msg){
 if(commands.has(msg.id)||(msg.expires_at&&msg.expires_at<(Date.now()+offset)/1000))return;
 commands.add(msg.id);if(commands.size>200)commands.delete(commands.values().next().value);
 if(msg.action==='reload'){location.reload();return}if(msg.action==='home')home();if(msg.action==='expand')openWidget(msg.widget_id);
 if(msg.action==='select_date'){selectedDate=msg.date;month=msg.date.slice(0,7);followToday=msg.date===state.today;await render({preserveScroll:false})}
 requestAnimationFrame(()=>socket?.send({type:'ack',id:msg.id,status:'rendered'}));
}
$('#backButton').onclick=home;
async function start(){
 if(demo){
  $('#demoBadge').classList.remove('hidden');applyState(structuredClone(window.ROOM_DEMO));connection(true,'데모 모드');
  window.addEventListener('message',e=>{
   if(e.source!==parent)return;
   if(e.data?.type==='room-demo-state')applyState(e.data.state);
   if(e.data?.type==='room-demo-command')remote(e.data.command);
   if(e.data?.type==='room-demo-completion-result'){
    const request=demoRequests.get(e.data.requestId);if(!request)return;
    clearTimeout(request.timer);demoRequests.delete(e.data.requestId);
    if(e.data.error){const err=new Error(e.data.error);err.status=e.data.status;request.reject(err)}else request.resolve(e.data.task);
   }
  });return;
 }
 const pair=new URLSearchParams(location.hash.slice(1)).get('pair');
 if(pair){history.replaceState(null,'',location.pathname);try{await R.api('/api/devices/claim','POST',{code:pair})}catch(e){requirePairing('연결 실패');return}}
 await refresh();if(stopped)return;
 socket=R.connect('display',msg=>{
  if(msg.type==='hello'){presence();refresh()}
  if(msg.type==='invalidate')refresh();if(msg.type==='command')remote(msg);
  if(msg.type==='revoked')requirePairing('연결이 해제되었습니다');
 },ok=>connection(ok));
}
setInterval(()=>{
 if(!state||stopped)return;
 const p=R.dateParts(now(),state.settings.timezone),today=`${p.year}-${p.month}-${p.day}`;
 document.querySelectorAll('[data-clock-main]').forEach(el=>el.textContent=p.hour+':'+p.minute);
 document.querySelectorAll('[data-clock-second]').forEach(el=>el.textContent=p.second);
 document.querySelectorAll('[data-clock-date]').forEach(el=>el.textContent=R.prettyDate(today));
 if(today!==state.today){state.today=today;if(followToday){selectedDate=today;month=today.slice(0,7)}render()}
},1000);
setInterval(()=>{if(!demo){refresh();presence()}},30000);
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>render(),130)});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!demo)refresh()});
window.RoomDisplay={selectDate,openWidget,home,getState:()=>state};start();
})();
