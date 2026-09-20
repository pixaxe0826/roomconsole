// Read-only, one request/response per view. No model calls or write APIs.
// Selection is per widget instance and never persisted to the server.
const LABELS={awaiting_confirmation:'관리자 확인 필요',needs_clarification:'요청 확인 필요',running:'응답 대기',succeeded:'응답 완료',failed:'전송·응답 실패',cancelled:'요청 취소',interrupted:'처리 중단'};
function model(ctx){
 const v=ctx.viewState,feed=ctx.state.llm_display;
 const items=feed?.enabled&&Array.isArray(feed.items)?feed.items:[];
 if(v.followLatest===undefined)v.followLatest=true;
 if(!v.roots)v.roots=new Set();
 if(v.followLatest)v.selectedId=items[items.length-1]?.id||null;
 let index=items.findIndex(i=>i.id===v.selectedId);
 if(index<0&&items.length){
  index=Math.min(Math.max(v.positionHint||0,0),items.length-1);
  v.selectedId=items[index].id;
 }
 if(!items.length){v.selectedId=null;v.detail=null;v.detailKey=null;}
 v.positionHint=Math.max(index,0);
 const item=items[index]||null,key=item?[item.id,item.updated_at,item.status].join('|'):null;
 return {v,feed,items,index,item,key,data:v.detail?.id===item?.id?v.detail:null};
}
function timestamp(ctx,value){
 try{return new Intl.DateTimeFormat('ko-KR',{timeZone:ctx.state.settings.timezone,month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value));}catch{return '';}
}
function answer(data,status){
 if(status==='running')return {text:'요청을 처리하고 있습니다.',placeholder:true};
 if(data?.output)return {text:data.output,placeholder:false};
 if(data?.refusal)return {text:data.refusal,placeholder:false};
 const text={failed:'응답을 받지 못했습니다. 관리자에서 상세 내용을 확인해 주세요.',cancelled:'요청이 취소되었습니다. 자동으로 다시 전송하지 않습니다.',interrupted:'처리가 중단되었습니다. 관리자에서 기록을 확인해 주세요.'}[status]
  ||(data?.output_kind==='tool_only'?'도구 호출 응답만 받았습니다. 실행하지 않았습니다.':data?.output_kind==='no_final_output'?'최종 답변이 없습니다. 관리자에서 응답 내용을 확인해 주세요.':'비어 있는 응답입니다. 관리자에서 기록을 확인해 주세요.');
 return {text,placeholder:true};
}
export function render(ctx){
 const m=model(ctx),{v,item,data}=m,{esc,icon}=ctx.util;
 const error=v.failedKey===m.key&&v.error,waiting=item&&!error&&(!data||v.pendingKey===m.key);
 const status=item?.status||'empty',label=LABELS[status]||'응답 확인';
 const canPrev=m.index>0,canNext=m.index>=0&&m.index<m.items.length-1;
 const caption=item?timestamp(ctx,item.sent_at):'LOCAL ASSISTANT';
 let body;
 if(!ctx.state.capabilities?.llm_response_widget&&!ctx.isDemo){body='<div class="llmr-empty"><strong>서버 업데이트가 필요해요</strong><span>관리자에서 위젯 업데이트를 적용해 주세요.</span></div>';}
 else if(!item){body='<div class="llmr-empty"><strong>아직 처리한 요청이 없어요</strong><span>관리자 음성 수신함에서 요청을 보내 주세요.<br>조회는 실제 데이터, 변경은 관리자 확인 후 반영합니다.</span></div>';}
 else if(!data){body=`<div class="llmr-empty" role="status"><strong>${error?'기록을 불러오지 못했어요':ctx.connected?'요청과 응답을 불러오는 중…':'서버 연결을 기다리고 있어요'}</strong><span>${error?esc(v.error):'이 화면에서는 요청을 새로 전송하지 않습니다.'}</span>${error?'<button type="button" data-stop data-llmr-action="retry" class="llmr-retry">다시 불러오기</button>':''}</div>`;}
 else{
  const a=answer(data,status);
  body=`<div class="llmr-panels ${ctx.expanded?'llmr-full':''}"><section class="llmr-pane llmr-request"><div class="llmr-pane-label">보낸 요청</div><div class="llmr-scroll" ${ctx.expanded?'data-stop tabindex="0"':''} data-llmr-scroll="input"><div class="llmr-text" data-llmr-input>${esc(data.input)}</div></div></section><section class="llmr-pane llmr-answer"><div class="llmr-pane-label">비서 응답 <span class="llmr-status" data-status="${esc(status)}">${esc(label)}</span></div><div class="llmr-scroll" ${ctx.expanded?'data-stop tabindex="0"':''} data-llmr-scroll="output"><div class="llmr-text ${a.placeholder?'llmr-placeholder':''}" data-llmr-output>${esc(a.text)}</div>${data.truncated?'<small class="llmr-warning">출력 한도에 도달했습니다. 답변이 완전하지 않을 수 있습니다.</small>':''}</div></section></div>`;
 }
 const stateNote=!ctx.connected?'연결 끊김 · 마지막 기록':error?'갱신 실패 · 마지막 기록':waiting&&data?'갱신 중':ctx.expanded?(data?.model||'요청 · 응답 조회'):'눌러서 전체 보기';
 return `<div class="widget-content llmr-content ${ctx.expanded?'expanded':''} ${ctx.compact?'llmr-compact':''}" data-llmr-selected="${esc(item?.id||'')}" aria-busy="${!!waiting}"><div class="llmr-head"><h2>${icon('note',17)} ${esc(ctx.instance.title||'LLM 응답')}</h2><time datetime="${esc(item?.sent_at||'')}">${esc(caption)}</time><button type="button" data-stop data-llmr-action="expand" class="llmr-expand" aria-label="LLM 요청과 응답 크게 보기">${icon('expand',16)}</button></div>${body}<div class="llmr-nav" data-stop><button type="button" data-llmr-action="older" class="llmr-arrow llmr-prev" aria-label="이전 요청과 응답" ${canPrev?'':'disabled'}>${icon('chevron',19)}</button><div class="llmr-nav-center"><span class="llmr-counter" aria-live="polite">${item?m.index+1:0} <span>/ ${m.items.length}</span></span>${!v.followLatest&&item?'<button type="button" data-llmr-action="latest" class="llmr-latest">최신으로</button>':''}<span class="llmr-nav-note">${esc(stateNote)}</span>${error&&data?'<button type="button" class="llmr-latest" data-llmr-action="retry">다시 조회</button>':''}</div><button type="button" data-llmr-action="newer" class="llmr-arrow" aria-label="다음 요청과 응답" ${canNext?'':'disabled'}>${icon('chevron',19)}</button></div></div>`;
}
async function fetchEntry(ctx,id,signal){
 if(ctx.isDemo){
  const value=ctx.state.llm_display_demo_details?.[id];
  if(!value){const e=Error('예시 기록이 없습니다.');e.status=404;throw e;}
  return structuredClone(value);
 }
 const res=await fetch('/api/display/llm/'+encodeURIComponent(id),{credentials:'same-origin',cache:'no-store',signal});
 if(!res.ok){const e=Error('기록을 확인할 수 없습니다.');e.status=res.status;throw e;}
 const body=await res.json();
 if(body.id!==id||typeof body.input!=='string'||(body.output!==null&&typeof body.output!=='string'))throw Error('응답 형식을 확인할 수 없습니다.');
 return body;
}
function load(ctx,m,force=false){
 const {v,key,item}=m;if(!key||(!ctx.connected&&!ctx.isDemo))return;
 if(!force&&(v.detailKey===key||v.pendingKey===key||v.failedKey===key))return;
 v.abort?.abort();const controller=new AbortController();v.abort=controller;
 const seq=(v.seq||0)+1;v.seq=seq;v.pendingKey=key;v.error=null;v.failedKey=null;
 const timer=setTimeout(()=>controller.abort(),10000);
 fetchEntry(ctx,item.id,controller.signal).then(data=>{
  if(v.seq!==seq)return;
  v.detail=data;v.detailKey=key;v.failedKey=null;
 }).catch(e=>{
  if(v.seq!==seq)return;
  v.failedKey=key;
  v.error=e.status===401?'연결 권한이 만료되었습니다. 관리자에서 다시 연결해 주세요.':e.status===403?'위젯 공유가 해제되었습니다. 화면을 새로고침해 주세요.':e.status===404?'삭제되었거나 더 이상 표시할 수 없는 기록입니다.':e.name==='AbortError'?'서버 응답 시간이 초과되었습니다.':'서버 연결을 확인한 뒤 다시 불러와 주세요.';
  // A revoked/deleted record must not remain visible under a success label.
  if([401,403,404].includes(e.status)){v.detail=null;v.detailKey=null;}
 }).finally(()=>{
  clearTimeout(timer);
  if(v.seq!==seq)return;
  v.pendingKey=null;v.abort=null;
  if(v.roots.size)ctx.redraw();
 });
}
export function bind(root,ctx){
 const m=model(ctx),v=m.v;v.roots.add(root);
 // Periodic state refresh retries a transient failed GET at most once per new
 // snapshot. A local render must never create an infinite fetch/re-render loop.
 const refreshStamp=ctx.state.server_time;
 if(v.refreshStamp!==refreshStamp){v.refreshStamp=refreshStamp;v.failedKey=null;}
 const area=ctx.expanded?'expanded':'grid';
 const restoreFrame=requestAnimationFrame(()=>{
  // bind runs before the new widget node is attached. Detached scroll boxes
  // clamp scrollTop to zero, so restore only after layout has a real viewport.
  if(!root.isConnected)return;
  root.querySelectorAll('[data-llmr-scroll]').forEach(el=>{el.scrollTop=v.scroll?.[area+':'+m.item?.id+':'+el.dataset.llmrScroll]||0;});
 });
 const click=e=>{
  const button=e.target.closest('[data-llmr-action]');if(!button||!root.contains(button))return;
  e.stopPropagation();if(button.disabled)return;
  const a=button.dataset.llmrAction,now=model(ctx);
  if(a==='expand'){ctx.expand();return;}
  if(a==='retry'){v.failedKey=null;load(ctx,now,true);ctx.redraw();return;}
  let index=now.index;
  if(a==='latest'){v.followLatest=true;index=now.items.length-1;}
  if(a==='older'){index=Math.max(0,index-1);v.followLatest=false;}
  if(a==='newer'){index=Math.min(now.items.length-1,index+1);v.followLatest=index===now.items.length-1;}
  if(index>=0){v.selectedId=now.items[index].id;v.positionHint=index;v.failedKey=null;ctx.redraw();}
 };
 root.addEventListener('click',click);
 load(ctx,m);
 return()=>{
  cancelAnimationFrame(restoreFrame);
  root.removeEventListener('click',click);
  v.scroll=v.scroll||{};
  // Capture the ID at bind time, not a newly selected ID from a button callback.
  root.querySelectorAll('[data-llmr-scroll]').forEach(el=>{v.scroll[area+':'+m.item?.id+':'+el.dataset.llmrScroll]=el.scrollTop;});
  v.roots.delete(root);
  queueMicrotask(()=>{
   // During a normal redraw the new grid/focus roots have already been bound.
   if(v.roots.size)return;
   v.seq=(v.seq||0)+1;v.abort?.abort();v.abort=null;v.pendingKey=null;v.detail=null;v.detailKey=null;v.failedKey=null;
  });
 };
}
