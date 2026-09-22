function clock(seconds){const s=Math.max(0,Math.ceil(seconds));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;}
function total(minutes,seconds){
 if(!/^\d+$/.test(String(minutes))||!/^\d+$/.test(String(seconds)))return null;
 const m=Number(minutes),s=Number(seconds),n=m*60+s;
 return Number.isInteger(m)&&m<=10&&s<=59&&n>=1&&n<=600?n:null;
}
function slots(ctx){return ctx.state.layout.widgets.filter(w=>w.type==='timers').slice().sort((a,b)=>a.y-b.y||a.x-b.x||a.id.localeCompare(b.id));}
export function render(ctx){
 const E=ctx.util.esc,v=ctx.viewState,items=ctx.state.timers?.items||[],placed=slots(ctx),ids=new Set(placed.map(w=>w.id));
 // Stable layout instance ID, never the workspace's latest/current timer.
 const own=items.find(t=>t.widget_id===ctx.instance.id),running=own?.state==='running';
 const fallback=ctx.instance.config?.duration_seconds,configured=own?.duration_seconds??(Number.isInteger(fallback)&&fallback>=1&&fallback<=600?fallback:240);
 if(v.timerMinutes===undefined){v.timerMinutes=String(Math.floor(configured/60));v.timerRemainder=String(configured%60);}
 if(running){v.timerMinutes=String(Math.floor(own.duration_seconds/60));v.timerRemainder=String(own.duration_seconds%60);v.timerDirty=false;}
 const n=total(v.timerMinutes,v.timerRemainder),state=running?'running':(!own||v.timerDirty?'idle':own.state);
 const enabled=!ctx.isDemo&&ctx.connected&&ctx.state.capabilities?.timer_control;
 const orphan=placed[0]?.id===ctx.instance.id?items.filter(t=>t.state==='running'&&!ids.has(t.widget_id)):[];
 const status=state==='running'?'실행 중':state==='expired'?'시간 종료':state==='stopped'?'종료됨':'대기';
 const sound=window.RoomTimers?.soundStatus(ctx.instance.id)||'소리 꺼짐';
 return `<div class="widget-content rh-timers ${ctx.expanded?'expanded':''} ${ctx.compact?'compact':''}" data-stop>
  <div class="widget-head"><h2><span class="timer-number">${placed.findIndex(w=>w.id===ctx.instance.id)+1}</span>${E(ctx.instance.title||'타이머')}</h2><button type="button" class="timer-sound" data-timer-sound aria-label="이 타이머 ${E(sound)}" title="활성 페이지에서만 소리가 납니다" ${ctx.isDemo?'disabled':''}>${sound==='소리 허용됨'?'소리 켬':'소리 끔'}</button></div>
  <div class="timer-readout" ${own?`data-timer-id="${E(own.id)}"`:''}>
   <div class="timer-count" data-timer-main data-timer-count data-deadline="${E(own?.deadline_at||'')}" data-state="${state}" data-duration="${own?.duration_seconds||n||0}">${state==='idle'?(n===null?'—:—':clock(n)):clock(own.remaining_seconds)}</div>
   <small data-timer-status>${ctx.isDemo?'미리보기':!ctx.connected?'오프라인 · 마지막 상태':status}</small>
  </div>
  <form class="timer-start" novalidate>
   <div class="timer-fields"><label><input data-timer-duration data-timer-field="minutes" type="number" inputmode="numeric" min="0" max="10" step="1" value="${E(v.timerMinutes)}" aria-label="타이머 분" ${enabled&&!running&&!v.timerBusy?'':'disabled'}><span>분</span></label><label><input data-timer-duration data-timer-field="seconds" type="number" inputmode="numeric" min="0" max="59" step="1" value="${E(v.timerRemainder)}" aria-label="타이머 초" ${enabled&&!running&&!v.timerBusy?'':'disabled'}><span>초</span></label></div>
   <div class="timer-actions"><button class="btn primary" type="submit" ${enabled&&!running&&!v.timerBusy?'':'disabled'}>${own&&!running?'다시 시작':'시작'}</button><button class="btn" type="button" data-timer-stop="${E(own?.id||'')}" data-version="${own?.version||1}" ${enabled&&running&&!v.timerBusy?'':'disabled'}>종료</button></div>
  </form>
  <p class="timer-message" role="status">${E(v.timerMessage||'')}</p>
  ${orphan.length?`<details class="timer-recovery"><summary>이전·제거된 위젯 타이머 ${orphan.length}개</summary>${orphan.map(t=>`<div class="timer-recovery-row" data-timer-id="${E(t.id)}"><span>${E(t.label)} <b data-timer-count data-state="running" data-deadline="${E(t.deadline_at)}" data-duration="${t.duration_seconds}">${clock(t.remaining_seconds)}</b></span><button class="btn small" type="button" data-timer-legacy data-timer-stop="${E(t.id)}" data-version="${t.version}" ${enabled&&!v.timerBusy?'':'disabled'}>종료</button></div>`).join('')}</details>`:''}
 </div>`;
}
export function bind(root,ctx){
 const v=ctx.viewState,minutes=root.querySelector('[data-timer-field="minutes"]'),seconds=root.querySelector('[data-timer-field="seconds"]'),main=root.querySelector('[data-timer-main]');
 function update(){root.querySelectorAll('[data-timer-count][data-state="running"]').forEach(el=>{
  const at=window.RoomTimers?.now?.()??Date.parse(ctx.state.timers?.server_time||ctx.now.toISOString());
  const n=Math.max(0,Math.min(Number(el.dataset.duration),Math.ceil((Date.parse(el.dataset.deadline)-at)/1000)));
  if(Number.isFinite(n))el.textContent=clock(n);
  const status=el.parentElement.querySelector('[data-timer-status]');
  if(status&&!ctx.isDemo)status.textContent=!ctx.connected?'오프라인 · 마지막 상태':window.RoomTimers?.needsSync?.()?'시각 동기화 중':n===0?'완료 확인 중':'실행 중';
 });}
 function edit(){
  v.timerMinutes=minutes.value;v.timerRemainder=seconds.value;v.timerDirty=true;v.timerMessage='';
  const n=total(minutes.value,seconds.value);main.dataset.state='idle';main.textContent=n===null?'—:—':clock(n);
  root.querySelector('[data-timer-status]').textContent=n===null?'1초~10분을 입력하세요':'설정 시간';
  root.querySelector('.timer-message').textContent='';
 }
 minutes.addEventListener('input',edit);seconds.addEventListener('input',edit);
 async function operate(action,body,target,legacy=false){
  if(v.timerBusy||ctx.isDemo||!ctx.connected)return;
  v.timerBusy=true;v.timerMessage='처리 중…';ctx.redraw();
  try{
   const result=await window.RoomTimers.mutate(legacy?null:ctx.instance.id,action,body,target);
   v.timerMessage=result.duplicate?'이미 처리한 요청입니다. 다시 실행하지 않았습니다.':'';
   if(action==='start'&&!result.duplicate)v.timerDirty=false;
  }catch(e){v.timerMessage=e.message;}
  finally{v.timerBusy=false;ctx.redraw();}
 }
 const form=root.querySelector('.timer-start');form.onsubmit=e=>{
  e.preventDefault();if(root.querySelector('button[type="submit"]').disabled)return;
  // Read this form's current fields, not a cached/global duration.
  const n=total(minutes.value,seconds.value);
  if(n===null){v.timerMessage='1초~10분만 설정할 수 있습니다. 초는 0~59로 입력하세요.';ctx.redraw();return;}
  operate('start',{duration_seconds:n});
 };
 const click=e=>{const b=e.target.closest('button');if(!b||b.disabled)return;e.stopPropagation();
  if(b.hasAttribute('data-timer-stop'))operate('stop',{version:Number(b.dataset.version)},b.dataset.timerStop,b.hasAttribute('data-timer-legacy'));
  if(b.hasAttribute('data-timer-sound')){
   if(window.RoomTimers?.soundStatus(ctx.instance.id)==='소리 허용됨'){window.RoomTimers.mute(ctx.instance.id);ctx.redraw();}
   else window.RoomTimers?.enableSound(ctx.instance.id).then(()=>ctx.redraw());
  }
 };
 root.addEventListener('click',click);update();const interval=setInterval(update,200);
 return()=>{clearInterval(interval);root.removeEventListener('click',click);minutes.removeEventListener('input',edit);seconds.removeEventListener('input',edit);};
}
