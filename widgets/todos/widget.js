export function render(ctx) {
  const {esc,icon,sortedTasks,prettyDate,addDays,dayDate,cat}=ctx.util;
  const date=ctx.selectedDate,items=sortedTasks(ctx.state,date),done=items.filter(t=>t.completed).length;
  if(ctx.compact)return `<div class="widget-content compact"><div class="compact-title">${esc(ctx.instance.title||'할 일')}</div><div class="compact-number">${items.length-done}<small class="small muted"> 남음</small></div><div class="compact-summary">${esc(items.find(t=>!t.completed)?.title||'가벼운 하루예요')}</div></div>`;
  // getUTCDay is Sunday=0. Never use the recurrence API's Monday=0 convention here.
  const start=addDays(date,-dayDate(date).getUTCDay()),end=addDays(start,6);
  const range=`${prettyDate(start,{year:'numeric',month:'numeric',day:'numeric'})} – ${prettyDate(end,{...(start.slice(0,4)!==end.slice(0,4)?{year:'numeric'}:{}),month:'numeric',day:'numeric'})}`;
  const days=Array.from({length:7},(_,i)=>{
    const d=addDays(start,i),weekday=['일','월','화','수','목','금','토'][i];
    return `<button type="button" data-stop data-date="${d}" class="day-chip ${d===date?'active':''} ${d===ctx.state.today?'is-today':''} ${i===0?'is-sunday':i===6?'is-saturday':''}" aria-label="${esc(prettyDate(d))}" aria-pressed="${d===date}" ${d===ctx.state.today?'aria-current="date"':''}><span>${weekday}</span><b>${Number(d.slice(-2))}</b></button>`;
  }).join('');
  const rows=items.map(t=>{
    const pending=!!ctx.taskPending?.(t.id),enabled=!!ctx.canCompleteTasks&&!pending;
    const action=t.completed?'완료 취소':'완료 처리';
    return `<div class="todo-item ${t.completed?'done':''}" data-task-id="${esc(t.id)}"><button type="button" data-stop data-complete-task="${esc(t.id)}" data-next-completed="${!t.completed}" class="task-complete-button" role="checkbox" aria-checked="${t.completed}" aria-label="${esc(t.title)} · ${action}" aria-busy="${pending}" title="${pending?'저장 중':ctx.canCompleteTasks?action:'서버에 연결해야 변경할 수 있습니다'}" ${enabled?'':'disabled'}><span class="task-circle ${t.completed?'done':''}" aria-hidden="true">${pending?icon('refresh',12):t.completed?icon('check',12):''}</span></button><div class="grow"><div class="task-title">${esc(t.title)}</div><div class="task-meta"><span>${esc(t.time||'하루 중 언제든')}</span>${t.series_id?icon('repeat',11):''}${t.priority==='high'?'<span>중요</span>':''}${pending?'<span class="task-saving" role="status">저장 중…</span>':''}</div>${ctx.expanded&&t.notes?`<p class="small muted" style="white-space:pre-wrap;line-height:1.7;margin-top:8px">${esc(t.notes)}</p>`:''}</div>${cat(t.category)}</div>`;
  }).join('');
  return `<div class="widget-content todo-content ${ctx.expanded?'expanded':''}"><div class="widget-head"><div><h2>${icon('todo')} ${esc(ctx.instance.title||'할 일')}</h2><div class="todo-head-date">${date===ctx.state.today?'오늘 · ':''}${esc(prettyDate(date,{year:'numeric',month:'long',day:'numeric'}))}</div></div><span class="badge green">${items.length-done}개 남음</span></div><div class="todo-week-label"><span>${esc(range)}</span><button type="button" data-stop data-week-today class="todo-today-button" aria-label="오늘의 할 일로 이동">오늘</button></div><nav class="day-strip todo-week-strip" aria-label="할 일 주간 날짜 선택" data-week-start="${start}" data-week-end="${end}"><button type="button" data-stop data-week-step="-1" class="week-arrow" aria-label="이전 주" title="이전 주"><span class="week-arrow-left">${icon('chevron',18)}</span></button>${days}<button type="button" data-stop data-week-step="1" class="week-arrow" aria-label="다음 주" title="다음 주">${icon('chevron',18)}</button></nav><div class="todo-list">${rows||`<div class="empty">${icon('leaf',34)}<strong>비워 둔 하루도 좋아요</strong><span>이 날짜에 등록된 할 일이 없습니다.</span></div>`}</div><div class="todo-progress"><div class="row between"><span>${done} / ${items.length} 완료</span><span>${items.length?Math.round(done/items.length*100):0}%</span></div><div class="progress"><span style="width:${items.length?done/items.length*100:0}%"></span></div></div></div>`;
}

export function bind(root,ctx) {
  const handler=e=>{
    const b=e.target.closest('[data-date],[data-week-step],[data-week-today],[data-complete-task]');
    if(!b||!root.contains(b))return;
    e.stopPropagation();
    if(b.disabled)return;
    if(b.hasAttribute('data-complete-task')){
      // Click only, not touchstart+click: one touch causes one explicit state change.
      ctx.setTaskCompletion?.(b.dataset.completeTask,b.dataset.nextCompleted==='true');
    }else if(b.hasAttribute('data-week-step')){
      // Preserve the selected weekday while moving whole weeks, without a 7-day limit.
      ctx.selectDate(ctx.util.addDays(ctx.selectedDate,7*Number(b.dataset.weekStep)));
    }else if(b.hasAttribute('data-week-today'))ctx.selectDate(ctx.state.today);
    else ctx.selectDate(b.dataset.date);
  };
  root.addEventListener('click',handler);
  return()=>root.removeEventListener('click',handler);
}
