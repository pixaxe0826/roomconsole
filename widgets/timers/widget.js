function clock(seconds){const s=Math.max(0,Math.ceil(seconds));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;}
export function render(ctx){
 const E=ctx.util.esc,v=ctx.viewState,data=ctx.state.timers||{items:[],active_count:0},items=data.items||[];
 const seconds=v.timerSeconds??ctx.instance.config?.duration_seconds??240;
 const current=items.find(t=>t.id===data.current_id)||items[0],shown=ctx.expanded?items:(current?[current]:[]);
 const enabled=!ctx.isDemo&&ctx.connected&&ctx.state.capabilities?.timer_control;
 return `<div class="widget-content rh-timers ${ctx.expanded?'expanded':''} ${ctx.compact?'compact':''}">
  <div class="widget-head"><h2>${ctx.util.icon('clock')} ${E(ctx.instance.title||'타이머')}</h2><span class="eyebrow">${data.active_count||0}개 실행</span></div>
  <form class="timer-start" data-stop><label><span>초 <small>1–600</small></span><input data-timer-duration type="number" inputmode="numeric" min="1" max="600" step="1" value="${E(seconds)}" aria-label="타이머 시간 (초)" required ${enabled?'':'disabled'}></label><button class="btn primary" type="submit" ${enabled&&!v.timerBusy?'':'disabled'}>시작</button></form>
  <p class="timer-message" role="status">${E(v.timerMessage|| (ctx.isDemo?'미리보기 · 실제 타이머는 서버에서 시작하세요.':!ctx.connected?'오프라인 · 상태를 확인할 수 없습니다.':''))}</p>
  <div class="timer-items todo-list">${shown.map(t=>`<div class="timer-row" data-timer-id="${E(t.id)}"><div class="timer-info"><strong>${E(t.label)} ${t.id===data.current_id?'<small>현재</small>':''}</strong><div class="timer-count" data-timer-count data-deadline="${E(t.deadline_at)}" data-state="${E(t.state)}" data-duration="${t.duration_seconds}">${clock(t.remaining_seconds)}</div><small data-timer-status>${t.state==='running'?'실행 중':t.state==='expired'?'시간 종료':'종료됨'}</small></div>${t.state==='running'?`<button class="btn small" data-stop data-timer-stop="${E(t.id)}" data-version="${t.version}" ${enabled&&!v.timerBusy?'':'disabled'}>종료</button>`:''}</div>`).join('')||'<p class="muted">실행 중인 타이머가 없습니다.</p>'}</div>
  ${!ctx.expanded&&items.length?`<button data-stop data-timer-expand class="timer-expand">전체 ${items.length}개 보기</button>`:''}
  <div class="timer-sound" data-stop><button class="btn small" data-timer-sound="enable" ${ctx.isDemo?'disabled':''}>소리 허용</button><button class="btn small" data-timer-sound="mute">음소거</button><small>${E(window.RoomTimers?.soundStatus()||'소리 꺼짐')}</small></div>
  ${ctx.expanded?'<p class="timer-caveat">현재 = 가장 최근 시작한 실행 중 타이머. 소리는 활성 페이지에서만 사용합니다. 잠금·백그라운드 알림은 지원하지 않습니다.</p>':''}
 </div>`;
}
export function bind(root,ctx){
 const v=ctx.viewState,input=root.querySelector('[data-timer-duration]');
 function update(){root.querySelectorAll('[data-timer-count]').forEach(el=>{
  const at=window.RoomTimers?.now?.()??ctx.now.getTime();
  const seconds=el.dataset.state==='running'?Math.max(0,Math.min(Number(el.dataset.duration),Math.ceil((Date.parse(el.dataset.deadline)-at)/1000))):0;
  el.textContent=clock(seconds);
  if(el.dataset.state==='running'&&seconds===0)el.parentElement.querySelector('[data-timer-status]').textContent='완료 확인 중';
 });}
 input.addEventListener('input',()=>{v.timerSeconds=input.value;v.timerMessage='';});
 async function operate(action,body,target){
  if(v.timerBusy||ctx.isDemo||!ctx.connected)return;
  v.timerBusy=true;v.timerMessage='처리 중…';ctx.redraw();
  try{const result=await window.RoomTimers.mutate(ctx.instance.id,action,body,target);v.timerMessage=result.duplicate?'이미 처리한 요청입니다. 목록을 확인하세요.':action==='start'?'타이머를 시작했습니다.':result.changed?'타이머를 종료했습니다.':'이미 끝난 타이머입니다.';}
  catch(e){v.timerMessage=e.message;}
  finally{v.timerBusy=false;ctx.redraw();}
 }
 const form=root.querySelector('.timer-start');form.onsubmit=e=>{e.preventDefault();const n=Number(v.timerSeconds??input.value);if(!Number.isInteger(n)||n<1||n>600){v.timerMessage='1~600 사이의 정수 초를 입력하세요.';ctx.redraw();return;}operate('start',{duration_seconds:n});};
 const click=e=>{const b=e.target.closest('button');if(!b)return;
  if(b.hasAttribute('data-timer-stop')){e.stopPropagation();operate('stop',{version:Number(b.dataset.version)},b.dataset.timerStop);}
  if(b.hasAttribute('data-timer-expand')){e.stopPropagation();ctx.expand();}
  if(b.dataset.timerSound==='enable'){e.stopPropagation();window.RoomTimers?.enableSound().then(()=>ctx.redraw());}
  if(b.dataset.timerSound==='mute'){e.stopPropagation();window.RoomTimers?.mute();ctx.redraw();}
 };
 root.addEventListener('click',click);update();const interval=setInterval(update,250);
 return()=>{clearInterval(interval);root.removeEventListener('click',click);};
}
