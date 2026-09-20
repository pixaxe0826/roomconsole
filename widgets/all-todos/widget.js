// No task copy or new endpoint: same occurrence IDs and completion-only permission.
const CHUNK=80;
function model(ctx){
 const view=ctx.viewState||(ctx.viewState={});
 if(!['all','open','done'].includes(view.filter))view.filter='all';
 view.limit=Math.max(CHUNK,Number(view.limit)||CHUNK);
 const all=ctx.util.chronologicalTasks(ctx.state.tasks);
 return {view,all,items:all.filter(t=>ctx.util.taskStatusMatch(t,view.filter)),done:all.filter(t=>t.completed).length};
}
function rows(ctx,items,limit){
 const {esc,icon,cat,prettyDate}=ctx.util;let date='';
 return items.slice(0,limit).map(t=>{
  let header='';
  if(t.date!==date){date=t.date;header=`<div class="all-date-heading ${date===ctx.state.today?'is-today':''}" data-all-date="${esc(date)}"><time datetime="${esc(date)}">${esc(prettyDate(date,{year:'numeric',month:'long',day:'numeric',weekday:'short'}))}</time>${date===ctx.state.today?'<span>오늘</span>':''}</div>`}
  const pending=!!ctx.taskPending?.(t.id),allowed=!!ctx.canCompleteTasks&&!pending;
  const action=t.completed?'완료 취소':'완료 처리';
  return header+`<div class="all-task-row ${t.completed?'done':''}" data-task-id="${esc(t.id)}" data-task-date="${esc(t.date)}"><button type="button" data-stop data-complete-task="${esc(t.id)}" data-next-completed="${!t.completed}" class="all-complete" role="checkbox" aria-checked="${!!t.completed}" aria-busy="${pending}" aria-label="${esc(t.title)} · ${esc(t.date)} · ${action}" title="${pending?'저장 중':allowed?action:'서버에 연결해야 변경할 수 있습니다'}" ${allowed?'':'disabled'}><span aria-hidden="true">${pending?icon('refresh',13):t.completed?icon('check',13):''}</span></button><div class="grow"><div class="all-task-title">${esc(t.title)}</div><div class="all-task-meta"><time>${esc(t.time||'시간 미지정')}</time>${t.series_id?`<span title="반복 회차">${icon('repeat',11)} 반복</span>`:''}${t.priority==='high'?'<span>중요</span>':''}${pending?'<span role="status">저장 중…</span>':''}</div>${ctx.expanded&&t.notes?`<p class="all-task-notes">${esc(t.notes)}</p>`:''}</div>${cat(t.category)}</div>`;
 }).join('');
}
function content(ctx,m){
 const shown=Math.min(m.view.limit,m.items.length);
 return rows(ctx,m.items,shown)+(shown<m.items.length?`<button type="button" data-stop data-all-more class="all-load-more">다음 ${Math.min(CHUNK,m.items.length-shown)}개 이어 보기 <span>${shown.toLocaleString()} / ${m.items.length.toLocaleString()}</span></button>`:`<div class="all-list-end">${m.items.length?'모든 항목을 표시했어요':'해당하는 할 일이 없습니다.'}</div>`);
}
export function render(ctx){
 const m=model(ctx),{esc,icon}=ctx.util;
 if(ctx.compact)return `<div class="widget-content compact"><div class="compact-title">${esc(ctx.instance.title||'전체 할 일')}</div><div class="compact-number">${(m.all.length-m.done).toLocaleString()}<small class="small muted"> 남음</small></div><div class="compact-summary">전체 ${m.all.length.toLocaleString()}개 · 날짜순</div></div>`;
 return `<div class="widget-content all-todos-content ${ctx.expanded?'expanded':''}"><div class="widget-head"><div><div class="all-eyebrow">ALL TASKS · 날짜순</div><h2>${icon('todo')} ${esc(ctx.instance.title||'전체 할 일')}</h2></div><button type="button" data-stop data-all-expand class="all-expand" aria-label="전체 할 일 크게 보기">${icon('expand',18)}</button></div><div class="all-todos-summary"><strong>${m.all.length.toLocaleString()}<small>개 등록</small></strong><span>미완료 ${m.all.length-m.done} · 완료 ${m.done}</span></div><div class="all-toolbar" data-stop><div class="all-filters" role="group" aria-label="완료 상태 보기">${[['all','전체'],['open','미완료'],['done','완료']].map(([v,label])=>`<button type="button" data-all-filter="${v}" aria-pressed="${m.view.filter===v}">${label}</button>`).join('')}</div><button type="button" class="all-jump" data-all-today>오늘 근처</button><button type="button" class="all-jump" data-all-first>처음</button></div><div class="todo-list all-todos-list" data-stop tabindex="0" aria-label="날짜순 전체 할 일 목록">${content(ctx,m)}</div><div class="all-todos-footer"><span>과거부터 미래까지 · 시간 미지정은 날짜 끝</span><span data-all-count>${Math.min(m.view.limit,m.items.length)} / ${m.items.length}</span></div></div>`;
}
export function bind(root,ctx){
 const list=root.querySelector('.all-todos-list');if(!list)return;
 let disposed=false,scheduled=0;
 function repaint(){
  const top=list.scrollTop,m=model(ctx);list.innerHTML=content(ctx,m);list.scrollTop=top;
  root.querySelector('[data-all-count]').textContent=`${Math.min(m.view.limit,m.items.length)} / ${m.items.length}`;
 }
 function more(){const m=model(ctx);if(m.view.limit>=m.items.length)return;m.view.limit+=CHUNK;repaint()}
 const scroll=()=>{
  if(scheduled||disposed)return;
  scheduled=requestAnimationFrame(()=>{scheduled=0;if(!disposed&&list.clientHeight>0&&list.scrollHeight-list.scrollTop-list.clientHeight<180)more()});
 };
 const click=e=>{
  const b=e.target.closest('[data-all-filter],[data-complete-task],[data-all-more],[data-all-today],[data-all-first],[data-all-expand]');
  if(!b||!root.contains(b))return;e.stopPropagation();if(b.disabled)return;
  const m=model(ctx);
  if(b.hasAttribute('data-complete-task'))ctx.setTaskCompletion?.(b.dataset.completeTask,b.dataset.nextCompleted==='true');
  else if(b.hasAttribute('data-all-expand'))ctx.expand();
  else if(b.hasAttribute('data-all-filter')){m.view.filter=b.dataset.allFilter;m.view.limit=CHUNK;list.scrollTop=0;ctx.redraw()}
  else if(b.hasAttribute('data-all-more'))more();
  else if(b.hasAttribute('data-all-first'))list.scrollTop=0;
  else if(b.hasAttribute('data-all-today')){
   if(!m.items.length)return;
   let index=m.items.findIndex(t=>t.date>=ctx.state.today);if(index<0)index=m.items.length-1;
   m.view.limit=Math.max(m.view.limit,Math.ceil((index+1)/CHUNK)*CHUNK);repaint();
   const target=[...list.querySelectorAll('[data-task-date]')].find(el=>el.dataset.taskDate===m.items[index].date);
   if(target)list.scrollTop+=target.getBoundingClientRect().top-list.getBoundingClientRect().top-38;
  }
 };
 root.addEventListener('click',click);list.addEventListener('scroll',scroll,{passive:true});
 return()=>{disposed=true;cancelAnimationFrame(scheduled);root.removeEventListener('click',click);list.removeEventListener('scroll',scroll)};
}
