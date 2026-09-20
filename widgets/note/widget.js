export function render(ctx){
 const E=ctx.util.esc, legacy=ctx.state.widget_data?.note?.text??ctx.instance.config?.text??'';
 const items=[...(legacy?[{id:'legacy',title:ctx.instance.config?.caption||'기존 고정 메모',body:legacy,pinned:true}]:[]),...(ctx.state.life?.notes?.items||[])];
 const v=ctx.viewState||(ctx.viewState={}),idx=Math.max(0,items.findIndex(x=>x.id===v.noteId)),note=items[idx];if(note)v.noteId=note.id;
 return `<div class="widget-content life-notes ${ctx.expanded?'expanded':''} ${ctx.compact?'compact':''}"><div class="widget-head"><h2>${ctx.util.icon('note')} ${E(ctx.instance.title||'메모')}</h2><span class="eyebrow">${items.length?idx+1+' / '+items.length:'NOTES'}</span></div>${note?`<div class="life-note-body todo-list"><span class="life-note-pin">${note.pinned?'고정 메모':'메모'}</span><h3>${E(note.title)}</h3><p>${E(note.body||'내용 없음')}</p></div><div class="life-note-nav" data-stop><button class="btn small" data-note-dir="-1" ${idx===0?'disabled':''} aria-label="이전 메모">←</button><span>읽기 전용 · 관리자에서 편집</span><button class="btn small" data-note-dir="1" ${idx>=items.length-1?'disabled':''} aria-label="다음 메모">→</button></div>`:'<div class="empty"><strong>표시할 메모가 없습니다</strong><span>관리자 → 메모에서 작성하고<br>‘iPad에 표시’를 켜 주세요.</span></div>'}</div>`;
}
export function bind(root,ctx){
 const legacy=ctx.state.widget_data?.note?.text??ctx.instance.config?.text??'',items=[...(legacy?[{id:'legacy'}]:[]),...(ctx.state.life?.notes?.items||[])];
 const handler=e=>{const b=e.target.closest('[data-note-dir]');if(!b)return;e.stopPropagation();const i=Math.max(0,items.findIndex(n=>n.id===ctx.viewState.noteId)),n=items[i+Number(b.dataset.noteDir)];if(n){ctx.viewState.noteId=n.id;ctx.redraw();}};
 root.addEventListener('click',handler);return()=>root.removeEventListener('click',handler);
}
