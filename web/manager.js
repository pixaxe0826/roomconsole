(() => {
'use strict';
const R=Room,E=R.esc,I=R.icon,$=s=>document.querySelector(s),demo=!!window.ROOM_DEMO;
const tabs={overview:['대시보드','오늘의 방을 확인하고, 연결된 화면을 관리하세요.','grid','YOUR SPACE, AT A GLANCE'],tasks:['할 일','모든 등록 회차를 날짜순으로. 과거의 기록부터 앞으로의 계획까지.','todo','MAKE ROOM FOR WHAT MATTERS'],layout:['위젯과 배치','공간에 맞춰 위젯의 종류와 크기를 정하세요.','grid','A PLACE FOR EVERYTHING'],devices:['표시 기기','같은 네트워크의 iPad와 화면을 연결하고 제어하세요.','screen','YOUR ROOM, CONNECTED'],voice:['음성 수신함','음성 파일과 전사 텍스트를 받아, 실행 전에 검토하세요.','mic','FROM VOICE TO ACTION'],llm:['LLM 인터페이스','음성에서 전사한 입력을 보내고, 요청별 출력과 성능을 확인하세요.','note','FROM TRANSCRIPT TO RESPONSE'],notes:['메모','작은 생각과 중요한 정보를 기록하세요. 공개할 메모는 직접 선택합니다.','note','A PLACE FOR YOUR THOUGHTS'],alarms:['알람','서버가 예약하고, 연결된 iPad 화면이 알려드립니다.','clock','A MOMENT TO REMEMBER'],settings:['설정','위치, 표시 방식, 데이터 보관을 관리하세요.','settings','THE DETAILS OF YOUR SPACE']};
let state,overview,tab='overview',layoutDraft,selectedWidget,taskEditing=null,filterDate='',filterQuery='',showAll=true,taskStatus='all',taskLimit=100,taskScrollTop=0,socket,refreshing=false,previewCleanup,dirty=false,rendering=false,renderQueued=false;
// Pairing is UI state, not transient HTML. Keep tokens in this tab's memory only.
// Pairing fix retained from 0.1.1-pairfix1; all-task-list release 0.1.2
let pairDraft={name:'Room iPad',baseUrl:demo?'http://192.168.0.20:8088':location.origin};
let pairSession=null,pairPending=false,pairError='',pairRequest=0;
let demoState=demo?structuredClone(window.ROOM_DEMO):null;
let demoOverview=demo?{devices:[{id:'demo-ipad',name:'거실 iPad Pro',online:true,revoked:0,viewport:'1194x834',last_seen:demoState.server_time,view:'home',created_at:demoState.server_time}],events:[{event:'layout.updated',detail:'기본 배치 적용',created_at:demoState.server_time},{event:'task.created',detail:'책 20페이지 읽기 · 7개',created_at:demoState.server_time},{event:'device.paired',detail:'거실 iPad Pro',created_at:demoState.server_time}],voice:[{id:'voice-demo',request_id:'demo-request',source:'adapter-preview',kind:'text',text:'내일 할 일에 택배 보내기 추가해 줘',locale:'ko-KR',status:'pending_review',created_at:demoState.server_time}],stats:{connected:1,tasks:demoState.tasks.length}}:null;
function demoDates(b){const r=b.repeat||{frequency:'none'},start=b.date,until=r.frequency==='none'?start:r.until;if(!until||until<start)throw Error('반복 종료일을 확인하세요.');const dates=[],anchor=R.addDays(start,-((R.dayDate(start).getUTCDay()+6)%7)),origin=R.dayDate(start);let d=start,steps=0;while(d<=until&&steps++<1830){const dt=R.dayDate(d),n=Math.round((dt-origin)/86400000),week=Math.floor((dt-R.dayDate(anchor))/86400000/7),wd=(dt.getUTCDay()+6)%7,interval=Number(r.interval||1),mon=(dt.getUTCFullYear()-origin.getUTCFullYear())*12+dt.getUTCMonth()-origin.getUTCMonth(),last=new Date(Date.UTC(dt.getUTCFullYear(),dt.getUTCMonth()+1,0)).getUTCDate();let ok=r.frequency==='none'||r.frequency==='daily'&&n%interval===0||r.frequency==='weekdays'&&wd<5&&week%interval===0||r.frequency==='weekly'&&(r.weekdays?.length?r.weekdays:[(origin.getUTCDay()+6)%7]).includes(wd)&&week%interval===0||r.frequency==='monthly'&&mon%interval===0&&dt.getUTCDate()===Math.min(origin.getUTCDate(),last);if(ok)dates.push(d);d=R.addDays(d,1)}if(!dates.length)throw Error('조건에 맞는 반복 날짜가 없습니다.');return dates}
function validateLayout(l){const cells=new Set(),ids=new Set();if(l.columns<4||l.columns>16||l.rows<4||l.rows>16)throw Error('격자는 각 축 4~16칸이어야 합니다.');if(!l.widgets.length)throw Error('위젯이 하나 이상 필요합니다.');for(const w of l.widgets){if(ids.has(w.id))throw Error('위젯 ID 중복');ids.add(w.id);if(w.x<0||w.y<0||w.w<1||w.h<1||w.x+w.w>l.columns||w.y+w.h>l.rows)throw Error('위젯이 격자를 벗어납니다.');for(let y=w.y;y<w.y+w.h;y++)for(let x=w.x;x<w.x+w.w;x++){const p=x+','+y;if(cells.has(p))throw Error('위젯끼리 겹칩니다. 위치를 조정해 주세요.');cells.add(p)}}}
async function api(path,method='GET',body){if(!demo)return R.api(path,method,body);const url=new URL(path,'http://demo'),p=url.pathname,random=()=>Math.random().toString(36).slice(2)+Date.now().toString(36);let result={ok:true};
if(p==='/api/state')return structuredClone(demoState);if(p==='/api/admin/overview')return structuredClone({...demoOverview,widgets:demoState.widgets,widget_errors:[]});
if(p==='/api/tasks/preview'){const dates=demoDates(body);return {dates,count:dates.length}}
if(p==='/api/tasks'&&method==='POST'){const dates=demoDates(body),sid=body.repeat?.frequency!=='none'?random():null,ids=[];for(const date of dates){const id=random();ids.push(id);demoState.tasks.push({...body,id,date,series_id:sid,completed:false,version:1,created_at:demoState.server_time,updated_at:demoState.server_time})}result={count:dates.length,ids,series_id:sid}}
else if(/^\/api\/tasks\/[^/]+\/completion$/.test(p)&&method==='PATCH'){
 const t=demoState.tasks.find(t=>t.id===p.split('/')[3]);
 if(!t){const e=Error('작업이 없습니다.');e.status=404;throw e}
 if(t.version!==body.version){const e=Error('다른 화면에서 변경되었습니다.');e.status=409;throw e}
 if(typeof body.completed!=='boolean')throw Error('완료 값이 올바르지 않습니다.');
 if(t.completed!==body.completed){t.completed=body.completed;t.version++}result=t;
}
else if(p.startsWith('/api/tasks/')&&method==='PATCH'){const t=demoState.tasks.find(t=>t.id===p.split('/').pop());if(!t)throw Error('작업이 없습니다.');Object.assign(t,body,{version:t.version+1});result=t}
else if(p.startsWith('/api/tasks/')&&method==='DELETE'){const t=demoState.tasks.find(t=>t.id===p.split('/').pop()),scope=url.searchParams.get('scope');const before=demoState.tasks.length;demoState.tasks=demoState.tasks.filter(x=>!(x.id===t.id||(t.series_id&&x.series_id===t.series_id&&(scope==='series'||scope==='future'&&x.date>=t.date))));result={deleted:before-demoState.tasks.length}}
else if(p==='/api/layout'){validateLayout(body);demoState.layout={...structuredClone(body),version:body.version+1};result=demoState.layout}
else if(p==='/api/settings'){demoState.settings=structuredClone(body);result=body}
else if(p==='/api/commands'){const frame=$('#previewFrame');frame?.contentWindow.postMessage({type:'room-demo-command',command:{...body,id:random()}},'*');const dev=demoOverview.devices.find(d=>!d.revoked);if(dev)dev.view=body.action==='expand'?body.widget_id:'home';return {delivered_connections:frame?1:0,id:random()}}
else if(p==='/api/devices/pair'){const id=random();demoOverview.devices.push({id,name:body.name,online:false,revoked:0,created_at:demoState.server_time});result={device_id:id,path:'/client#pair=demo-'+random(),expires_in:600}}
else if(p.startsWith('/api/devices/')&&method==='DELETE'){const d=demoOverview.devices.find(d=>d.id===p.split('/').pop());if(d){d.revoked=1;d.online=false}}
else if(p==='/api/voice/text'){const v={...body,id:random(),kind:'text',status:'pending_review',created_at:demoState.server_time};demoOverview.voice.unshift(v);result={id:v.id,status:v.status}}
else if(p==='/api/voice/upload'){const file=body.get('file'),isText=file.type==='text/plain'||file.name.endsWith('.txt'),v={id:random(),request_id:body.get('request_id'),source:body.get('source'),kind:isText?'text':'audio',text:isText?await file.text():'',status:isText?'pending_review':'awaiting_transcription',created_at:demoState.server_time};demoOverview.voice.unshift(v);result={id:v.id,status:v.status}}
else if(p.startsWith('/api/voice/')&&method==='PATCH'){const v=demoOverview.voice.find(v=>v.id===p.split('/').pop());Object.assign(v,body)}
else if(p.startsWith('/api/voice/')&&method==='DELETE'){demoOverview.voice=demoOverview.voice.filter(v=>v.id!==p.split('/').pop())}
else if(p==='/api/admin/integration')return {ingest_token:'DEMO_ONLY_NOT_A_REAL_KEY',schema_version:'1',auto_execute:false,transcription_enabled:false};
else if(p==='/api/widgets/reload')return {widgets:demoState.widgets,errors:[]};
else if(p==='/api/auth/logout'){R.toast('미리보기에는 실제 로그인이 없습니다.');return result}
demoState.revision++;demoOverview.stats.tasks=demoState.tasks.length;demoOverview.events.unshift({event:'demo.updated',detail:'미리보기에서 변경됨',created_at:demoState.server_time});return structuredClone(result)}
const input=(name,label,value='',type='text',attrs='')=>`<label>${label}<input name="${name}" type="${type}" value="${E(value??'')}" ${attrs}></label>`;
const select=(name,label,options,value,attrs='')=>`<label>${label}<select name="${name}" ${attrs}>${options.map(([k,v])=>`<option value="${E(k)}" ${String(value)===String(k)?'selected':''}>${E(v)}</option>`).join('')}</select></label>`;
const categories=[['personal','개인'],['work','업무'],['home','생활'],['study','학습']];
const statusLabel=s=>({pending_review:'검토 대기',awaiting_transcription:'전사 대기',reviewed:'검토 완료',dismissed:'보류',transcription_queued:'전사 대기열',transcribing:'V35 전사 중',transcription_failed:'전사 실패',transcription_cancelled:'전사 취소'}[s]||s);
const eventLabel=e=>({'layout.updated':'위젯 배치를 적용했어요','task.created':'새 할 일이 추가되었어요','task.updated':'할 일을 변경했어요','task.completion':'할 일 완료 상태를 변경했어요','task.deleted':'할 일을 삭제했어요','device.paired':'새 화면이 연결되었어요','device.pair_created':'기기 연결 링크를 만들었어요','device.revoked':'기기 연결이 해제되었어요','settings.updated':'설정을 저장했어요','weather.refreshed':'날씨 데이터를 갱신했어요','voice.received':'새 음성 입력이 도착했어요','voice.reviewed':'음성 입력을 검토했어요','demo.updated':'미리보기 내용을 변경했어요'}[e]||e);
function timeLabel(s){try{return new Intl.DateTimeFormat('ko-KR',{timeZone:state.settings.timezone,month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(s))}catch{return '—'}}
function taskRows(items,mini=false){if(!items.length)return `<div class="empty">${I('leaf',28)}<strong>등록된 할 일이 없어요</strong><span>관리자에서 새로운 계획을 추가해 보세요.</span></div>`;return items.map(t=>mini?`<div class="mini-task ${t.completed?'done':''}"><span class="status-circle ${t.completed?'done':''}">${t.completed?I('check',11):''}</span><div class="grow"><strong>${E(t.title)}</strong><small>${E(t.time||'하루 중 언제든')} ${t.series_id?'· 반복':''}</small></div>${R.cat(t.category)}</div>`:`<div class="manager-task ${t.completed?'done':''}" data-task-id="${E(t.id)}" data-task-date="${E(t.date)}"><button class="complete-button ${t.completed?'done':''}" data-op="complete" data-id="${t.id}" aria-label="${t.completed?'미완료로 변경':'완료 처리'}">${t.completed?I('check',14):''}</button><div class="grow"><div class="task-name">${E(t.title)}</div><div class="task-detail"><span>${showAll?E(t.date)+' · ':''}${E(t.time||'시간 미지정')}</span>${t.series_id?I('repeat',11):''}${t.priority==='high'?'<span>중요</span>':''}${R.cat(t.category)}</div></div><div class="task-buttons"><button class="icon-button" data-op="edit-task" data-id="${t.id}" aria-label="수정">${I('edit',15)}</button><button class="icon-button" data-op="delete-task" data-id="${t.id}" aria-label="삭제">${I('trash',15)}</button></div></div>`).join('')}
function renderOverview(){const items=R.sortedTasks(state,state.today),done=items.filter(t=>t.completed).length,pending=overview.voice.filter(v=>['pending_review','awaiting_transcription'].includes(v.status)).length,connected=overview.devices.filter(d=>d.online&&!d.revoked).length;const kpis=[['오늘의 할 일',items.length,'개',`${done}개 완료 · ${items.length-done}개 남음`,'todo'],['연결된 화면',connected,'대',connected?'표시 기기와 연결되어 있어요':'iPad 연결 링크를 만들어 주세요','screen'],['사용 중인 위젯',state.layout.widgets.length,'개',`${state.layout.columns} × ${state.layout.rows} 격자에 배치`,'grid'],['검토할 입력',pending,'개',pending?'실행 전 확인을 기다리고 있어요':'음성 수신 준비 완료','mic']];
return `<div class="kpis">${kpis.map(([label,num,unit,sub,icon])=>`<div class="kpi"><div class="kpi-top">${label}<span class="kpi-icon">${I(icon,16)}</span></div><div class="kpi-value">${num}<small>${unit}</small></div><div class="kpi-sub">${E(sub)}</div></div>`).join('')}</div><div class="overview-columns"><div><section class="panel"><div class="panel-head"><div><h2>지금, 내 방의 화면</h2><p>iPad와 동일한 구성으로 미리 봅니다.</p></div><button class="btn small" data-op="go" data-tab="layout">배치 편집 ${I('arrow',13)}</button></div><div class="device-preview" id="previewBox"><iframe id="previewFrame" title="iPad 화면 미리보기" ${demo?'':'src="/client"'}></iframe></div><div class="preview-label"><span>${I('screen',13)} iPad Pro 11 · ${state.layout.columns}×${state.layout.rows} GRID</span><span>가로 화면 · 완료 체크 가능</span></div></section><section class="panel activity"><div class="panel-head"><h2>최근 변경 사항</h2><span class="small muted">서버 기록</span></div><div class="activity-list">${overview.events.slice(0,4).map(e=>`<div class="activity-row"><span></span><div class="grow">${E(eventLabel(e.event))}${e.detail?` <span class="muted">· ${E(e.detail.slice(0,45))}</span>`:''}</div><time>${E(timeLabel(e.created_at))}</time></div>`).join('')||'<p class="muted small">아직 기록이 없습니다.</p>'}</div></section></div><div class="stack"><section class="panel"><div class="panel-head"><div><h2>화면 바로 제어</h2><p>연결된 화면에 즉시 반영됩니다.</p></div></div><div class="action-list">${[['home','home','대시보드로 돌아가기','모든 위젯을 한눈에'],['calendar','calendar','달력 크게 보기','이번 달의 계획 확인'],['tasks','todo','할 일 크게 보기','선택한 날에 집중하기']].map(([action,icon,title,sub])=>`<button class="action-card" data-op="remote" data-action="${action==='home'?'home':'expand'}" data-id="${action}"><span>${I(icon,19)}</span><div><strong>${title}</strong><small>${sub}</small></div><span class="arrow">${I('chevron',13)}</span></button>`).join('')}</div></section><section class="panel"><div class="panel-head"><h2>오늘의 계획</h2><span class="badge green">${items.length}개</span></div><div class="mini-list">${taskRows(items.slice(0,5),true)}</div><button class="btn soft small" data-op="go" data-tab="tasks" style="width:100%;margin-top:15px">모든 할 일 관리 ${I('arrow',13)}</button></section><p class="note-line" style="padding:0 8px">${I('info',12)} 정보는 서버에 저장됩니다.<br>iPad에서는 날짜 조회·위젯 확대·할 일 완료 체크가 가능합니다.</p></div></div>`}
function taskForm(){const t=taskEditing?state.tasks.find(t=>t.id===taskEditing):null;return `<section class="panel"><div class="panel-head"><div><h2>${t?'할 일 수정':'새로운 할 일'}</h2><p>${t?.series_id?'이 날짜의 회차만 수정합니다.':'작은 계획부터 시작해 보세요.'}</p></div>${t?`<button class="icon-button" data-op="new-task" aria-label="새 작업으로">${I('close',16)}</button>`:I('plus',18)}</div><form id="taskForm"><div class="stack">${input('title','할 일 제목',t?.title||'','text','required maxlength="240" placeholder="무엇을 할 예정인가요?"')}<div class="form-grid">${input('date','날짜',t?.date||filterDate||state.today,'date','required')}${input('time','시간 · 선택',t?.time||'','time')}</div><div class="form-grid">${select('category','분류',categories,t?.category||'personal')}${select('priority','우선순위',[['normal','보통'],['high','중요']],t?.priority||'normal')}</div><label>메모 · 선택<textarea name="notes" maxlength="4000" placeholder="기억할 내용을 적어 주세요">${E(t?.notes||'')}</textarea></label></div>${!t?`<hr class="form-divider"><div class="section-label">${I('repeat',15)} 반복 설정</div>${select('frequency','반복 주기',[['none','반복하지 않음'],['daily','매일'],['weekdays','평일 · 월~금'],['weekly','매주 · 요일 선택'],['monthly','매월']], 'none')}<div id="repeatFields" class="hidden top-space"><div class="form-grid">${input('interval','간격',1,'number','min="1" max="52" disabled')}${input('until','마지막 날짜 · 포함',filterDate||state.today,'date','required disabled')}</div><p class="field-help">일 / 주 / 월 단위 간격입니다. 종료일에 해당하는 회차도 포함합니다.</p><div id="weekdayFields" class="hidden"><div class="weekdays">${['월','화','수','목','금','토','일'].map((d,i)=>`<label class="weekday-check"><input type="checkbox" name="weekdays" value="${i}">${d}</label>`).join('')}</div><p class="field-help">선택하지 않으면 시작 날짜의 요일로 반복합니다.</p></div></div><div id="repeatPreview" class="recurrence-box">반복 작업은 날짜별로 생성되며,<br>각 날짜의 완료 상태를 따로 관리합니다.</div>`:`<p class="field-help top-space">반복 규칙 변경은 필요한 이후 회차를 삭제한 뒤 새 규칙으로 다시 생성하세요.</p>`}<p id="taskError" class="form-error top-space"></p><div class="form-actions">${!t?'<button class="btn" type="button" data-op="preview-repeat">날짜 미리보기</button>':''}<button class="btn primary grow" type="submit">${t?'변경 저장':'할 일 추가'}</button></div></form></section>`}
// A single chronological list, progressively expanded without a hard 300-row cutoff.
const TASK_CHUNK=100;
function taskItems(){return R.chronologicalTasks(state.tasks.filter(t=>(showAll||t.date===filterDate)&&R.taskStatusMatch(t,taskStatus)&&(!filterQuery||t.title.toLowerCase().includes(filterQuery.toLowerCase()))))}
function taskListHTML(items){
 const shown=items.slice(0,taskLimit);let date='';
 const body=shown.map(t=>{let heading='';if(date!==t.date){date=t.date;heading=`<div class="manager-date-heading ${date===state.today?'is-today':''}" data-manager-date="${E(date)}"><time datetime="${E(date)}">${E(R.prettyDate(date,{year:'numeric',month:'long',day:'numeric',weekday:'short'}))}</time>${date===state.today?'<span class="badge green">오늘</span>':''}</div>`}return heading+taskRows([t])}).join('');
 if(!items.length)return '<div class="empty">'+I('leaf',28)+'<strong>해당하는 할 일이 없어요</strong><span>기간·상태·검색 조건을 확인하거나 새 계획을 추가해 보세요.</span></div>';
 return body+(shown.length<items.length?`<button class="btn soft task-more" type="button" data-op="tasks-more">다음 ${Math.min(TASK_CHUNK,items.length-shown.length)}개 이어 보기 · ${shown.length} / ${items.length}</button>`:'<div class="manager-list-end">모든 항목을 표시했어요</div>');
}
function updateTaskList(){
 const list=$('#taskRows');if(!list)return;
 const top=list.scrollTop,items=taskItems();list.innerHTML=taskListHTML(items);list.scrollTop=top;taskScrollTop=list.scrollTop;
 const summary=$('#taskListSummary');if(summary)summary.textContent=`전체 ${state.tasks.length.toLocaleString()}개 · 조건 일치 ${items.length.toLocaleString()}개 · 표시 ${Math.min(taskLimit,items.length).toLocaleString()}개`;
 const badge=$('#taskMatchCount');if(badge)badge.textContent=items.length.toLocaleString()+'개';
}
function resetTaskBrowse(){taskLimit=TASK_CHUNK;taskScrollTop=0;const list=$('#taskRows');if(list)list.scrollTop=0;render()}
function bindTaskBrowse(){
 const list=$('#taskRows');if(!list)return;list.scrollTop=taskScrollTop;
 list.onscroll=()=>{taskScrollTop=list.scrollTop;if(list.clientHeight>0&&list.scrollHeight-list.scrollTop-list.clientHeight<160&&taskLimit<taskItems().length){taskLimit+=TASK_CHUNK;updateTaskList()}};
}
function renderTasks(){const items=taskItems();return `<div class="split"><section class="panel manager-tasks-panel"><div class="panel-head"><div><h2>${showAll?'모든 할 일':E(R.prettyDate(filterDate,{year:'numeric',month:'long',day:'numeric',weekday:'short'}))}</h2><p>과거·오늘·미래의 등록 회차 · 완료한 작업도 날짜순으로 유지합니다.</p></div><span id="taskMatchCount" class="badge green">${items.length.toLocaleString()}개</span></div><div class="task-toolbar all-task-toolbar">${select('showAll','기간',[['all','모든 날짜 · 기본'],['day','선택한 날짜']],showAll?'all':'day')}${input('filterDate','조회 날짜',filterDate,'date',showAll?'disabled title="기간을 선택한 날짜로 바꾸세요"':'')}${select('taskStatus','완료 상태',[['all','전체 · 완료 포함'],['open','미완료'],['done','완료']],taskStatus)}<label class="search-field">제목 검색<input name="filterQuery" value="${E(filterQuery)}" placeholder="전체 기간에서 찾기"></label></div><div class="manager-list-tools"><span id="taskListSummary">전체 ${state.tasks.length.toLocaleString()}개 · 조건 일치 ${items.length.toLocaleString()}개 · 표시 ${Math.min(taskLimit,items.length).toLocaleString()}개</span><div><button type="button" class="btn small" data-op="tasks-today">오늘 근처</button><button type="button" class="btn small" data-op="tasks-first">처음</button></div></div><div id="taskRows" class="manager-task-scroll" tabindex="0" aria-label="날짜순 할 일 전체 목록">${taskListHTML(items)}</div><p class="field-help">날짜 → 시간순 · 시간 미지정은 해당 날짜 끝 · 아래로 스크롤하면 이어집니다.</p></section>${taskForm()}</div>`}

function renderLayout(){if(!layoutDraft){layoutDraft=structuredClone(state.layout);selectedWidget=layoutDraft.widgets[0]?.id}const l=layoutDraft,w=l.widgets.find(w=>w.id===selectedWidget)||l.widgets[0];if(w)selectedWidget=w.id;const meta=state.widgets.find(m=>m.id===w?.type);let warning='';try{validateLayout(l)}catch(e){warning=e.message}
return `<div class="split"><div class="stack"><section class="panel"><div class="panel-head"><div><h2>화면 격자</h2><p>위젯을 드래그하거나 오른쪽 위치 값으로 이동합니다.</p></div><span class="badge">가로 × 세로</span></div><div class="layout-info">${input('gridColumns','가로 칸',l.columns,'number','min="4" max="16"')}${input('gridRows','세로 칸',l.rows,'number','min="4" max="16"')}<button class="btn small" data-op="reset-layout">기본 배치</button></div><div class="layout-board" id="layoutBoard" style="grid-template-columns:repeat(${l.columns},minmax(0,1fr));grid-template-rows:repeat(${l.rows},minmax(0,1fr));aspect-ratio:${l.columns}/${l.rows}">${Array.from({length:l.columns*l.rows},(_,i)=>`<div class="grid-cell" style="grid-column:${i%l.columns+1};grid-row:${Math.floor(i/l.columns)+1}"></div>`).join('')}${l.widgets.map(t=>`<button class="layout-tile ${t.id===selectedWidget?'selected':''}" draggable="true" data-wid="${t.id}" data-op="select-widget" style="grid-column:${t.x+1}/span ${t.w};grid-row:${t.y+1}/span ${t.h}">${I(state.widgets.find(m=>m.id===t.type)?.icon||'note',22)}<strong>${E(t.title||t.type)}</strong><small>${t.w} × ${t.h} · ${t.x},${t.y}</small></button>`).join('')}</div><p class="layout-warning">${E(warning)}</p><div class="layout-foot"><span>${l.widgets.length}개 위젯 · 겹침 없는 자유 배치</span><div class="row"><button class="btn small" data-op="discard-layout">되돌리기</button><button class="btn primary small" data-op="save-layout">화면에 적용 ${I('arrow',13)}</button></div></div></section><section class="panel"><div class="panel-head"><div><h2>위젯 라이브러리</h2><p>widgets/ 폴더의 manifest.json을 읽어 표시합니다.</p></div><button class="btn small" data-op="reload-widgets">${I('refresh',13)} 다시 검색</button></div><div class="widget-catalog">${state.widgets.map(m=>`<div class="catalog-card"><span style="color:var(--accent)">${I(m.icon,21)}</span><div class="grow"><strong>${E(m.name)}</strong><p>${E(m.description)}</p></div><button class="icon-button" data-op="add-widget" data-id="${m.id}" aria-label="${E(m.name)} 위젯 추가">${I('plus',16)}</button></div>`).join('')}</div>${state.widget_errors?.length?`<p class="form-error top-space">${E(state.widget_errors.map(e=>e.folder+': '+e.error).join('\n'))}</p>`:''}</section></div><section class="panel"><div class="panel-head"><div><h2>선택한 위젯</h2><p>${E(meta?.name||'위젯')} · ${E(w?.id||'')}</p></div>${I(meta?.icon||'note',20)}</div>${w?`<form id="widgetForm" class="stack">${select('widgetType','위젯 종류',state.widgets.map(m=>[m.id,m.name]),w.type)}${input('widgetTitle','표시 이름',w.title,'text','maxlength="80"')}<div class="form-grid">${input('widgetX','X · 왼쪽에서',w.x,'number','min="0" max="15"')}${input('widgetY','Y · 위에서',w.y,'number','min="0" max="15"')}${input('widgetW','가로 크기',w.w,'number','min="1" max="16"')}${input('widgetH','세로 크기',w.h,'number','min="1" max="16"')}</div></form><div class="size-presets">${[[1,1],[1,2],[1,3],[1,4],[2,2],[2,3],[2,4],[3,3],[3,4],[4,4]].map(([a,b])=>`<button class="size-preset ${w.w===a&&w.h===b?'selected':''}" data-op="size-widget" data-size="${a},${b}">${a}×${b}</button>`).join('')}</div><p class="field-help">크기는 가로×세로입니다. 작은 위젯은 요약 표시로 자동 전환됩니다. 직접 입력은 각 축 16칸까지 가능합니다.</p><hr class="form-divider"><label>위젯 설정 · JSON<textarea id="widgetConfig" spellcheck="false" rows="7" style="font-family:ui-monospace,monospace;font-size:11px">${E(JSON.stringify(w.config,null,2))}</textarea></label><button class="btn small" data-op="apply-widget-config" style="margin-top:10px">설정 초안 반영</button>${w.type==='llm-response'?'<p class="field-help">처리한 요청과 LLM 응답 또는 서버 확인 결과를 연결된 모든 표시 기기에 공유합니다. System 입력·추론·키는 공유하지 않습니다. 미전송·대기열 항목은 제외하며 위젯을 제거하면 조회가 차단됩니다.</p>':`<p class="field-help">서버 데이터는 /api/widgets/${E(w.type)}/data로 전달합니다.</p>`}<hr class="form-divider"><button class="btn danger" data-op="remove-widget">${I('trash',14)} 화면에서 위젯 제거</button>`:'<p class="muted">위젯을 추가해 주세요.</p>'}</section></div>`}
function renderDevices(){const devices=overview.devices.filter(d=>!d.revoked);return `<div class="split" id="devicesView"><div class="stack" id="deviceReadouts"><section class="panel"><div class="panel-head"><div><h2>연결된 화면</h2><p>한 작업 공간의 배치를 여러 기기에서 함께 표시합니다.</p></div><span class="badge green">${devices.length}대 등록</span></div>${devices.map(d=>`<article class="device-card"><div class="row"><div class="device-avatar">${I('screen',23)}</div><div class="grow"><h3>${E(d.name)}</h3><p>${E(d.viewport||'연결 정보 대기 중')} · ${d.online?'화면과 연결됨':'현재 오프라인'}</p></div><span class="badge ${d.online?'green':''}"><i class="status-dot ${d.online?'':'off'}"></i>${d.online?'온라인':'대기'}</span></div><div class="device-meta"><span>현재 화면: ${E(d.view||'—')}</span><span>${d.last_seen?E(timeLabel(d.last_seen)):'아직 접속하지 않음'}</span></div>${d.last_command?`<p>명령 ${E(d.last_command.status)} · ${E(timeLabel(d.last_command.at))}</p>`:''}<div class="form-actions"><button class="btn small" data-op="device-home" data-id="${d.id}">${I('home',13)} 홈으로</button><button class="btn small" data-op="device-reload" data-id="${d.id}">${I('refresh',13)} 다시 불러오기</button><button class="btn small danger" data-op="revoke-device" data-id="${d.id}">연결 해제</button></div></article>`).join('')||`<div class="empty">${I('screen',36)}<strong>첫 번째 화면을 연결하세요</strong><span>iPad를 서버와 같은 Wi-Fi에 연결한 뒤<br>오른쪽에서 연결 링크를 만드세요.</span></div>`}</section><section class="panel"><div class="panel-head"><h2>날짜 · 위젯 원격 제어</h2></div><form id="remoteForm" class="form-grid">${select('device_id','대상 화면',[['','연결된 모든 화면'],...devices.map(d=>[d.id,d.name])],'')}${input('date','표시할 날짜',state.today,'date','required')}${select('widget_id','확대할 위젯',[['','확대하지 않음'],...state.layout.widgets.map(w=>[w.id,w.title||w.type])],'')}<div class="form-actions" style="align-self:end;margin-top:0"><button class="btn primary" type="submit">화면에 보내기</button></div></form><p class="field-help top-space">오프라인 기기에는 명령을 쌓아 두지 않습니다. 수신 확인은 화면 처리 완료를 뜻하며, 사람이 읽었다는 뜻은 아닙니다.</p></section></div><section class="panel" id="pairPanel"><div class="panel-head"><div><h2>새 화면 연결</h2><p>일회용 링크는 10분 동안 유효합니다.</p></div>${I('plus',18)}</div><form id="pairForm" class="stack">${input('name','기기 이름',pairDraft.name,'text','required maxlength="80"')}${input('baseUrl','iPad에서 접속할 서버 주소',pairDraft.baseUrl,'url','required')}<p class="field-help">localhost 대신 서버의 실제 LAN IP를 입력하세요.<br>예: http://192.168.0.20:8088</p><button class="btn primary" type="submit">연결 링크 만들기</button></form><p id="pairError" class="form-error top-space" role="alert"></p><div id="pairResult"></div><div class="integration-note">${I('info',17)}<div>iPad Safari에서 링크를 열면 입력 없이 연결됩니다. 표시 기기는 조회·할 일 완료 상태 변경·음성 전송만 가능하며, 관리자 권한을 갖지 않습니다.</div></div></section></div>`}
function renderVoice(){const sp=overview.speech||{ready:false,reason:demo?'예시 화면 · 실제 엔진은 V35에서 실행됩니다.':'로컬 전사 엔진을 준비하세요.',model:'tiny',threads:2,max_seconds:30,queued:0,running:0};return `<div class="split"><section class="panel"><div class="panel-head"><div><h2>도착한 입력</h2><p>V35 로컬 전사 · 읽기 즉시 / 쓰기 확인 후 실행</p></div><span class="badge">최근 ${overview.voice.length}개</span></div><label class="voice-mode-label">요청 처리 방식<select id="voiceLLMMode">${[['auto','자동 분기 · 명령 또는 대화'],['chat','일반 대화만 · DB 실행 없음'],['legacy','기존 직접 전송 · 이전 System']].map(([k,v])=>`<option value="${k}" ${(window.RoomLLM?.getMode()||'auto')===k?'selected':''}>${v}</option>`).join('')}</select></label><p class="field-help">추가·완료·취소·삭제는 관리자에서 실제 대상을 확인한 뒤에만 실행합니다.</p>${overview.voice.map(v=>`<article class="voice-item"><div class="voice-meta"><span>${I(v.kind==='audio'?'mic':'note',13)} ${E(v.source)} · ${v.kind==='audio'?'음성 파일':'전사 텍스트'}</span><span>${E(timeLabel(v.created_at))}</span></div><h3>${E(v.text||'전사되지 않은 음성 파일입니다.')}</h3><div class="row" style="margin-top:10px"><span class="badge ${v.status==='pending_review'?'green':''}">${E(statusLabel(v.status))}</span></div>${v.kind==='audio'?`<textarea data-voice-transcript="${v.id}" placeholder="검토한 전사 내용을 입력하세요">${E(v.text)}</textarea><p class="field-help">${['queued','running'].includes(v.job_status)?'전사 중에는 수정·삭제 전에 취소해 주세요.':'전사는 틀릴 수 있습니다. 내용을 검토한 뒤 저장하세요.'}</p>${v.transcription_error?`<p class="speech-error">${E(v.transcription_error)}</p>`:''}${v.transcription_elapsed!=null?`<p class="field-help">서버 처리 ${Number(v.transcription_elapsed).toFixed(1)}초</p>`:''}`:''}<footer>${v.kind==='audio'?`${['queued','running'].includes(v.job_status)?`<button class="btn small" data-op="speech-cancel" data-id="${v.job_id}">전사 취소</button>`:v.job_status!=='succeeded'?`<button class="btn small primary" data-op="speech-enqueue" data-id="${v.id}" ${!sp.ready?'disabled':''}>${v.job_id?'전사 재시도':'V35에서 전사'}</button>`:''}<a href="/api/voice/${v.id}/audio" class="btn small" ${demo?'data-op="demo-download"':''}>파일 받기</a><button class="btn small" data-op="voice-transcript" data-id="${v.id}">전사 저장</button>`:''}<button class="btn primary small" data-op="voice-to-llm" data-id="${v.id}" ${!v.text?.trim()||['queued','running'].includes(v.job_status)?'disabled':''}>LLM으로 전송</button>${v.llm_count?`<button class="btn small" data-op="voice-llm-history" data-id="${v.id}">LLM 기록 ${v.llm_count}개</button>`:''}<button class="btn soft small" data-op="voice-to-task" data-id="${v.id}" ${!v.text?'disabled':''}>할 일 작성으로</button><button class="btn small" data-op="voice-reviewed" data-id="${v.id}">검토 완료</button><button class="btn small danger" data-op="voice-delete" data-id="${v.id}">삭제</button></footer></article>`).join('')||`<div class="empty">${I('mic',34)}<strong>수신 준비가 되었어요</strong><span>전사 텍스트 또는 음성 파일이 도착하면<br>이곳에서 검토할 수 있습니다.</span></div>`}</section><div class="stack"><section class="panel"><div class="panel-head"><h2>로컬 전사 엔진</h2><span class="badge ${sp.ready?'green':''}">${sp.ready?'준비됨':'설치 필요'}</span></div><div class="speech-engine"><div><strong>${E(sp.model)} · ${E(sp.language||'ko')}</strong><span>CPU ${sp.threads}스레드 · 녹음 최대 ${sp.max_seconds}초</span></div></div><p class="field-help">${E(sp.reason||'V35 내부에서 한 번에 한 파일씩 처리합니다.')}<br>대기 ${sp.queued}개 · 처리 중 ${sp.running}개</p><p class="field-help">iPad 마이크는 신뢰된 HTTPS의 클라이언트 상단 음성 입력에서 사용합니다. 녹음은 관리자에서 삭제할 때까지 서버에 보관합니다.</p></section><section class="panel"><div class="panel-head"><div><h2>텍스트 수신 테스트</h2><p>외부 음성인식 시스템의 입력을 가정합니다.</p></div></div><form id="voiceTextForm" class="stack">${input('source','입력 출처','manual','text','required maxlength="80"')}<label>전사된 내용<textarea name="text" required maxlength="16000" placeholder="예: 내일 할 일에 택배 보내기 추가해 줘"></textarea></label><button class="btn primary" type="submit">수신함에 넣기</button></form></section><section class="panel"><div class="panel-head"><div><h2>파일 수신 테스트</h2><p>음성 파일 또는 UTF-8 TXT를 받습니다.</p></div></div><form id="voiceFileForm" class="stack"><label>파일 · 최대 10MB<input name="file" type="file" accept=".wav,.mp3,.m4a,.mp4,.webm,.ogg,.flac,.aac,.txt" required></label><button class="btn" type="submit">파일 전송</button></form><div class="integration-note">${I('mic',16)}<div>음성 파일 → 수신함에서 전사 시작<br>텍스트 → 검토 대기<br><strong>받기만 하며, 명령을 실행하지 않습니다.</strong></div></div><button class="btn small" data-op="integration">연동 키 · API 보기</button><p class="field-help top-space">Alexa / Google Home의 실제 연결은 제공자별 인증과 어댑터가 추가로 필요합니다.</p></section></div></div>`}
function renderSettings(){const s=state.settings;return `<div class="settings-columns"><section class="panel"><div class="panel-head"><div><h2>허브 설정</h2><p>변경한 설정은 연결된 화면에 전달됩니다.</p></div>${I('settings',20)}</div><form id="settingsForm"><div class="stack">${input('title','화면 이름',s.title,'text','required maxlength="50"')}<div class="form-grid">${input('timezone','시간대 · IANA',s.timezone,'text','required placeholder="Asia/Seoul"')}${select('theme','화면 테마',[['light','밝은 화면'],['dark','어두운 화면']],s.theme)}</div><hr class="form-divider" style="margin:3px 0"><div><h3 style="font-size:14px">날씨 위치</h3><p class="field-help">정확한 집 주소 대신 도시 중심의 위도·경도만 입력해도 됩니다.</p></div>${input('location_name','표시할 지역 이름',s.location_name,'text','maxlength="60" placeholder="예: 서울"')}<div class="form-grid">${input('latitude','위도',s.latitude??'','number','step="any" min="-90" max="90" placeholder="37.5665"')}${input('longitude','경도',s.longitude??'','number','step="any" min="-180" max="180" placeholder="126.9780"')}</div>${input('weather_interval_minutes','날씨 갱신 간격 · 분',s.weather_interval_minutes,'number','min="5" max="120" required')}<p class="field-help">기본 15분입니다. 공급자 데이터의 갱신 주기에 따라 실제 관측 시각과 차이가 날 수 있습니다. 위치를 비우면 날씨 기능이 비활성화됩니다.</p></div><div class="form-actions"><button class="btn primary" type="submit">설정 저장</button><button class="btn" type="button" data-op="weather-refresh">${I('refresh',14)} 날씨 갱신</button></div></form></section><div class="stack"><section class="panel"><div class="panel-head"><h2>데이터 보관</h2>${I('download',18)}</div><div class="backup-card"><h3>JSON 내보내기</h3><p>할 일, 반복 규칙, 메모, 알람, 위젯 배치와 설정을 읽기 쉬운 파일로 저장합니다. 자동 가져오기 UI는 포함하지 않습니다.</p><a class="btn small" href="/api/admin/export" data-op="export-json">${I('download',13)} JSON 다운로드</a></div><div class="backup-card"><h3>데이터베이스 백업</h3><p>실행 중에도 일관된 SQLite 백업을 만듭니다. 음성 파일·키·위젯 코드는 별도 백업이 필요합니다.</p><a class="btn small" href="/api/admin/backup" ${demo?'data-op="demo-download"':''}>${I('download',13)} DB 다운로드</a></div><p class="note-line">전체 백업은 서버를 종료한 뒤 data/와 widgets/를 함께 복사하세요. 복원 절차는 README_KO.md를 확인하세요.</p></section><section class="panel"><div class="panel-head"><h2>운영 상태</h2><span class="badge green">v0.1.7</span></div><div class="info-list"><div class="info-row"><span>데이터 저장</span><strong>서버 SQLite</strong></div><div class="info-row"><span>클라이언트 입력</span><strong>조회 · 완료 상태 · 음성 전송</strong></div><div class="info-row"><span>음성 자동 실행</span><strong>사용하지 않음</strong></div><div class="info-row"><span>날씨 공급자</span><strong>Open-Meteo</strong></div><div class="info-row"><span>현재 데이터 버전</span><strong>${state.revision}</strong></div></div><div class="integration-note">${I('info',17)}<div>기본 HTTP 통신은 암호화되지 않습니다. 로컬망에서만 사용하고 인터넷에 포트를 직접 공개하지 마세요.</div></div></section></div></div>`}
const views={overview:renderOverview,tasks:renderTasks,layout:renderLayout,devices:renderDevices,voice:renderVoice,llm:()=>'<div id="llmWorkspace"></div>',notes:()=>'<div id="lifeWorkspace" data-kind="notes"></div>',alarms:()=>'<div id="lifeWorkspace" data-kind="alarms"></div>',settings:renderSettings};
function render(){
 if(!state)return;
 // A focused input can emit change/blur while its DOM is being replaced.
 // Defer a nested render until replacement has finished.
 if(rendering){renderQueued=true;return;}
 rendering=true;
 if($('#taskRows'))taskScrollTop=$('#taskRows').scrollTop;
 try{
  const info=tabs[tab];$('#pageTitle').textContent=info[0];$('#pageSubtitle').textContent=info[1];$('#pageEyebrow').textContent=info[3];$('#nav').innerHTML=Object.entries(tabs).map(([id,t])=>`<a href="#${id}" class="nav-item ${tab===id?'active':''}">${I(t[2],18)}<span class="nav-text">${t[0]}</span>${id==='voice'&&overview.voice.filter(v=>v.status==='pending_review').length?`<span class="nav-pill">${overview.voice.filter(v=>v.status==='pending_review').length}</span>`:''}</a>`).join('');previewCleanup?.();previewCleanup=null;const html=views[tab]();
  if(tab==='devices'&&$('#devicesView')&&$('#pairPanel')){
   // Updating only the left readouts keeps the QR image and form attached.
   const template=document.createElement('template');template.innerHTML=html;
   $('#deviceReadouts').replaceWith(template.content.querySelector('#deviceReadouts'));
  }else if(!(tab==='llm'&&$('#llmWorkspace'))&&!(['notes','alarms'].includes(tab)&&$('#lifeWorkspace')?.dataset.kind===tab)) $('#content').innerHTML=html;
  bindForms();bindTaskBrowse();syncPairPanel();bindPreview();
  if(tab==='llm') window.RoomLLM?.mount($('#llmWorkspace'));
  if(['notes','alarms'].includes(tab))window.RoomLife?.mount($('#lifeWorkspace'),tab);
 }finally{
  rendering=false;
  if(renderQueued){renderQueued=false;queueMicrotask(render);}
 }
}
function showLogin(){
 window.RoomLLM?.reset();window.RoomLife?.reset();
 socket?.close();pairRequest++;pairPending=false;pairSession=null;pairError='';
 pairDraft={name:'Room iPad',baseUrl:demo?'http://192.168.0.20:8088':location.origin};
 $('#pairResult')?.replaceChildren();
 $('#managerApp').classList.add('hidden');$('#login').classList.remove('hidden');
}
function refreshTaskReadouts(){
 // Refresh only readouts while a form is focused, never discard a draft.
 updateTaskList();
 const items=R.sortedTasks(state,state.today),done=items.filter(t=>t.completed).length;
 const mini=$('.mini-list');if(mini)mini.innerHTML=taskRows(items.slice(0,5),true);
 const value=$('.kpi .kpi-value');if(value)value.innerHTML=`${items.length}<small>개</small>`;
 const sub=$('.kpi .kpi-sub');if(sub)sub.textContent=`${done}개 완료 · ${items.length-done}개 남음`;
}
async function refresh(redraw=true){if(refreshing)return;refreshing=true;try{[state,overview]=await Promise.all([api('/api/state'),api('/api/admin/overview')]);if(!filterDate)filterDate=state.today;if(tab==='llm')window.RoomLLM?.refresh();if(['notes','alarms'].includes(tab))window.RoomLife?.refresh();if(redraw)render();else{refreshTaskReadouts();syncPairPanel();$('#previewFrame')?.contentWindow.postMessage(demo?{type:'room-demo-state',state}:{type:'no-op'},'*')}}catch(e){if(e.status===401)showLogin();else R.toast(e.message)}finally{refreshing=false}}
function navigate(next){if(!tabs[next])next='overview';if(tab==='layout'&&dirty&&next!=='layout'){/* Draft is intentionally retained until applied or explicitly discarded. */}tab=next;if(!demo)history.replaceState(null,'',location.pathname+location.search+'#'+next);render();window.scrollTo(0,0)}
function bindPreview(){
 const frame=$('#previewFrame'),box=$('#previewBox');
 if(!frame||!box)return;
 const width=1194,height=834;
 let observer=null,pending=0,disposed=false,lastScale=-1;
 let contentWidth=0,contentHeight=0;

 // CSS alone controls the preview box. Only transform its absolutely
 // positioned child; do not write box.width/height from this observer.
 function fit(){
  pending=0;
  if(disposed||!box.isConnected||!frame.isConnected)return;
  if(contentWidth<=0||contentHeight<=0)return;
  const scale=Math.min(contentWidth/width,contentHeight/height);
  if(!Number.isFinite(scale)||Math.abs(scale-lastScale)<0.0000001)return;
  lastScale=scale;
  frame.style.transform=`scale(${scale})`;
 }
 function schedule(w,h){
  if(disposed)return;
  contentWidth=w;contentHeight=h;
  if(!pending)pending=requestAnimationFrame(fit);
 }
 function measure(){
  // Preserve fractional CSS pixels; clientWidth/clientHeight round to integers.
  const css=getComputedStyle(box),px=name=>parseFloat(css[name])||0;
  const borderBox=css.boxSizing==='border-box';
  const w=px('width')-(borderBox?px('borderLeftWidth')+px('borderRightWidth')+px('paddingLeft')+px('paddingRight'):0);
  const h=px('height')-(borderBox?px('borderTopWidth')+px('borderBottomWidth')+px('paddingTop')+px('paddingBottom'):0);
  schedule(w,h);
 }

 async function onDemoCompletion(e){
  const message=e.data;
  if(!demo||disposed||e.source!==frame.contentWindow||message?.type!=='room-demo-completion')return;
  if(typeof message.id!=='string'||typeof message.requestId!=='string'||typeof message.completed!=='boolean'||!Number.isInteger(message.version))return;
  try{
   const task=await api('/api/tasks/'+encodeURIComponent(message.id)+'/completion','PATCH',{completed:message.completed,version:message.version});
   if(disposed)return;
   frame.contentWindow?.postMessage({type:'room-demo-completion-result',requestId:message.requestId,task},'*');
   await refresh(false);
   // Do not recreate the iframe: preserve its week, expanded view and scroll.
   const items=R.sortedTasks(state,state.today),done=items.filter(t=>t.completed).length;
   const list=$('.mini-list');if(list)list.innerHTML=taskRows(items.slice(0,5),true);
   const sub=$('.kpi .kpi-sub');if(sub)sub.textContent=`${done}개 완료 · ${items.length-done}개 남음`;
  }catch(err){
   if(!disposed){frame.contentWindow?.postMessage({type:'room-demo-completion-result',requestId:message.requestId,error:err.message,status:err.status},'*');await refresh(false)}
  }
 }
 if(demo)window.addEventListener('message',onDemoCompletion);
 if(demo&&window.ROOM_CLIENT_PREVIEW){
  frame.onload=()=>{
   if(!disposed)frame.contentWindow?.postMessage({type:'room-demo-state',state},'*');
  };
  frame.srcdoc=window.ROOM_CLIENT_PREVIEW.replace('window.ROOM_DEMO=','window.ROOM_EMBEDDED_DEMO=true;window.ROOM_DEMO=');
 }
 if(typeof ResizeObserver==='function'){
  observer=new ResizeObserver(entries=>{
   const entry=entries.find(item=>item.target===box);
   if(entry)schedule(entry.contentRect.width,entry.contentRect.height);
  });
  observer.observe(box);
 }else{
  window.addEventListener('resize',measure);
 }
 measure();
 previewCleanup=()=>{
  window.removeEventListener('message',onDemoCompletion);
  disposed=true;
  observer?.disconnect();
  if(pending)cancelAnimationFrame(pending);
  pending=0;
  window.removeEventListener('resize',measure);
  frame.onload=null;
 };
}
function pairingState(){
 const p=pairSession;if(!p)return 'empty';
 if(p.status==='active'){
  const device=overview?.devices.find(d=>d.id===p.deviceId);
  if(device?.revoked)p.status='revoked';
  else if(device?.last_seen||device?.online||overview?.events.some(e=>e.event==='device.paired'&&e.detail===p.deviceId))p.status='paired';
  else if(Date.now()>=p.expiresAt)p.status='expired';
  if(p.status!=='active')p.url=''; // Do not leave used/expired/revoked links in the DOM.
 }
 return p.status;
}
function pairingMarkup(status){
 const p=pairSession;if(!p)return '';
 if(status!=='active'){
  const messages={paired:['화면이 연결되었습니다.','이 링크는 사용되었습니다. 연결된 기기는 새 링크 없이 계속 사용할 수 있습니다.'],expired:['연결 링크가 만료되었습니다.','10분의 연결 시간이 지났습니다. 새 링크를 만들어 주세요.'],revoked:['연결이 해제되었습니다.','이 링크는 사용할 수 없습니다. 필요한 경우 새 링크를 만들어 주세요.']};
  const [title,description]=messages[status];
  return `<div class="pair-result" role="status" data-pair-status="${status}"><strong>${E(title)}</strong><p class="field-help top-space">${E(description)}</p></div>`;
 }
 return `<div class="pair-result" data-pair-status="active">${demo?'<div class="empty">'+I('screen',42)+'<span>데모 연결 링크 · 실제 기기에는 사용할 수 없습니다.</span></div>':`<img alt="iPad 연결 QR 코드" src="/api/admin/qr?url=${encodeURIComponent(p.url)}" referrerpolicy="no-referrer">`}<label>iPad Safari에서 여세요<input readonly value="${E(p.url)}" id="pairLink"></label><button class="btn small" data-op="copy-pair" style="margin-top:10px">링크 복사</button><p class="field-help"><strong id="pairCountdown"></strong> · 1회 사용<br>이 페이지의 자동 갱신과 탭 이동 후에도 링크를 유지합니다.<br>브라우저 새로고침·종료 후에는 새 링크를 만드세요.<br>이전 링크를 취소하려면 왼쪽에서 해당 기기의 연결을 해제하세요.</p></div>`;
}
function syncPairPanel(){
 if(tab!=='devices'||$('#managerApp').classList.contains('hidden'))return;
 const form=$('#pairForm'),result=$('#pairResult');if(!form||!result)return;
 const status=pairingState(),key=pairSession?`${pairSession.request}:${status}`:'empty';
 if(result.dataset.pairKey!==key){result.innerHTML=pairingMarkup(status);result.dataset.pairKey=key}
 const submit=form.querySelector('[type=submit]');submit.disabled=pairPending;
 submit.textContent=pairPending?'연결 링크 만드는 중…':'연결 링크 만들기';
 form.setAttribute('aria-busy',String(pairPending));
 for(const name of ['name','baseUrl'])form.elements[name].disabled=pairPending;
 $('#pairError').textContent=pairError;
 const countdown=$('#pairCountdown');if(countdown&&pairSession){
  const seconds=Math.max(0,Math.ceil((pairSession.expiresAt-Date.now())/1000));
  countdown.textContent=`남은 시간 ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
 }
}
async function submitPair(e){
 e.preventDefault();if(pairPending)return;
 const form=e.currentTarget;
 pairDraft={name:form.elements.name.value,baseUrl:form.elements.baseUrl.value};
 let base;
 try{
  base=new URL(pairDraft.baseUrl);
  if(!['http:','https:'].includes(base.protocol))throw Error('HTTP 또는 HTTPS 주소가 필요합니다.');
  if(!pairDraft.name.trim())throw Error('기기 이름을 입력하세요.');
 }catch(err){pairError=err.message;syncPairPanel();return}
 const request=++pairRequest,startedAt=Date.now();
 pairPending=true;pairError='';syncPairPanel();
 try{
  const out=await api('/api/devices/pair','POST',{name:pairDraft.name.trim()});
  // Ignore responses belonging to a logged-out/replaced session.
  if(request!==pairRequest)return;
  pairSession={request,deviceId:out.device_id,url:base.origin+out.path,
   expiresAt:startedAt+Math.max(0,Number(out.expires_in)||0)*1000,status:'active'};
  syncPairPanel();await refresh(false);syncPairPanel();
 }catch(err){
  if(request!==pairRequest)return;
  if(err.status===401){showLogin();return}
  pairError=err.message;R.toast(err.message);
 }finally{
  if(request===pairRequest){pairPending=false;syncPairPanel()}
 }
}
function taskBody(){const f=$('#taskForm'),fd=new FormData(f),frequency=fd.get('frequency')||'none',body={title:String(fd.get('title')||'').trim(),date:fd.get('date'),time:fd.get('time')||null,category:fd.get('category'),priority:fd.get('priority'),notes:fd.get('notes')||''};if(!taskEditing)body.repeat={frequency,interval:Number(fd.get('interval')||1),until:frequency==='none'?null:fd.get('until'),weekdays:fd.getAll('weekdays').map(Number)};return body}
async function saveMutation(path,method,body,message){const out=await api(path,method,body);await refresh();if(message)R.toast(typeof message==='function'?message(out):message);return out}
function bindForms(){const mode=$('#voiceLLMMode');if(mode)mode.onchange=()=>window.RoomLLM.setMode(mode.value);const f=$('#taskForm');if(f){f.onsubmit=async e=>{e.preventDefault();$('#taskError').textContent='';try{const t=state.tasks.find(t=>t.id===taskEditing),body=taskBody();if(t)await api('/api/tasks/'+t.id,'PATCH',{...body,version:t.version});else{const out=await api('/api/tasks','POST',body);R.toast(`${out.count}개의 할 일을 추가했습니다.`)}taskEditing=null;await refresh()}catch(err){$('#taskError').textContent=err.message}};f.elements.frequency?.addEventListener('change',()=>{const freq=f.elements.frequency.value,on=freq!=='none';$('#repeatFields').classList.toggle('hidden',!on);f.elements.until.disabled=!on;f.elements.interval.disabled=!on;$('#weekdayFields').classList.toggle('hidden',freq!=='weekly')})}
const filter=$('[name=filterDate]');if(filter)filter.onchange=()=>{filterDate=filter.value||state.today;resetTaskBrowse()};const all=$('[name=showAll]');if(all)all.onchange=()=>{showAll=all.value==='all';resetTaskBrowse()};const query=$('[name=filterQuery]');if(query)query.onchange=()=>{filterQuery=query.value;resetTaskBrowse()};const taskFilter=$('[name=taskStatus]');if(taskFilter)taskFilter.onchange=()=>{taskStatus=taskFilter.value;resetTaskBrowse()};
for(const [name,key] of [['gridColumns','columns'],['gridRows','rows']]){const field=$(`[name=${name}]`);if(field)field.onchange=()=>{layoutDraft[key]=Math.max(4,Math.min(16,Number(field.value)||4));dirty=true;render()}}
const wf=$('#widgetForm');if(wf)wf.onchange=()=>{const w=layoutDraft.widgets.find(w=>w.id===selectedWidget),fd=new FormData(wf);const nextType=fd.get('widgetType');if(nextType!==w.type){const meta=state.widgets.find(m=>m.id===nextType);if(!meta)return;w.type=nextType;w.title=meta.name;w.config=structuredClone(meta.configDefaults||{})}else w.title=fd.get('widgetTitle');for(const [name,key] of [['widgetX','x'],['widgetY','y'],['widgetW','w'],['widgetH','h']])w[key]=Number(fd.get(name));dirty=true;render()};
const board=$('#layoutBoard');if(board){let dragged;board.ondragstart=e=>{const tile=e.target.closest('[data-wid]');if(!tile)return;dragged=tile.dataset.wid;e.dataTransfer.setData('text/plain',dragged);e.dataTransfer.effectAllowed='move'};board.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='move'};board.ondrop=e=>{e.preventDefault();const w=layoutDraft.widgets.find(w=>w.id===(dragged||e.dataTransfer.getData('text/plain')));if(!w)return;const rect=board.getBoundingClientRect(),pad=parseFloat(getComputedStyle(board).paddingLeft),x=Math.floor((e.clientX-rect.left-pad)/(rect.width-2*pad)*layoutDraft.columns),y=Math.floor((e.clientY-rect.top-pad)/(rect.height-2*pad)*layoutDraft.rows);w.x=Math.max(0,Math.min(layoutDraft.columns-w.w,x));w.y=Math.max(0,Math.min(layoutDraft.rows-w.h,y));selectedWidget=w.id;dirty=true;render()}}
const pf=$('#pairForm');if(pf){
 pf.oninput=()=>{pairDraft={name:pf.elements.name.value,baseUrl:pf.elements.baseUrl.value}};
 pf.onsubmit=submitPair;
}
const rf=$('#remoteForm');if(rf)rf.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(rf),id=f.get('device_id')||null;await api('/api/commands','POST',{action:'select_date',date:f.get('date'),device_id:id});if(f.get('widget_id'))await api('/api/commands','POST',{action:'expand',widget_id:f.get('widget_id'),device_id:id});R.toast('온라인 화면에 날짜 명령을 보냈습니다.')}catch(err){R.toast(err.message)}};
const vt=$('#voiceTextForm');if(vt)vt.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(vt);await saveMutation('/api/voice/text','POST',{schema_version:'1',source:f.get('source'),request_id:'manual-'+Date.now()+'-'+Math.random().toString(36).slice(2),text:f.get('text'),locale:'ko-KR',metadata:{}},'수신함에 저장했습니다. 자동 실행하지 않습니다.')}catch(err){R.toast(err.message)}};
const vf=$('#voiceFileForm');if(vf)vf.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(vf);if(f.get('file').size>10*1024*1024)throw Error('파일은 최대 10MB입니다.');f.set('source','manual-upload');f.set('request_id','upload-'+Date.now());f.set('locale','ko-KR');await saveMutation('/api/voice/upload','POST',f,'파일을 수신했습니다. 음성은 전사 대기 상태로 보관합니다.')}catch(err){R.toast(err.message)}};
const sf=$('#settingsForm');if(sf)sf.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(sf),lat=f.get('latitude'),lon=f.get('longitude');if((lat==='')!==(lon===''))throw Error('위도와 경도를 함께 입력하세요.');await saveMutation('/api/settings','PUT',{title:f.get('title'),timezone:f.get('timezone'),theme:f.get('theme'),location_name:f.get('location_name'),latitude:lat===''?null:Number(lat),longitude:lon===''?null:Number(lon),weather_interval_minutes:Number(f.get('weather_interval_minutes'))},'설정을 저장했습니다.')}catch(err){R.toast(err.message)}};}
function modal(html){$('#modalContent').innerHTML=html;$('#modal').classList.remove('hidden');$('#modal').querySelector('button,input')?.focus()}
function closeModal(){$('#modal').classList.add('hidden');$('#modalContent').replaceChildren()}
function addWidget(type){const m=state.widgets.find(m=>m.id===type),l=layoutDraft,w=m.defaultSize?.w||2,h=m.defaultSize?.h||2;for(let y=0;y<=l.rows-h;y++)for(let x=0;x<=l.columns-w;x++){const candidate={id:type+'-'+Date.now().toString(36),type,title:m.name,x,y,w,h,config:structuredClone(m.configDefaults||{})};try{validateLayout({...l,widgets:[...l.widgets,candidate]});l.widgets.push(candidate);selectedWidget=candidate.id;dirty=true;render();return}catch{}}R.toast('이 크기의 빈 공간이 없습니다. 격자 세로 칸을 늘리거나 위젯을 줄여 주세요.')}
function downloadJSON(){const blob=new Blob([JSON.stringify({schema_version:1,settings:state.settings,layout:state.layout,tasks:state.tasks},null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='room-hub-demo-export.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
async function copyText(value,input){try{await navigator.clipboard.writeText(value);R.toast('복사했습니다.')}catch{input?.focus();input?.select();R.toast('이 브라우저에서는 자동 복사가 제한됩니다. 선택된 내용을 직접 복사해 주세요.')}}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-op]');if(!b)return;const op=b.dataset.op,id=b.dataset.id;try{
if(op==='go'){navigate(b.dataset.tab);return}if(op==='new-task'){taskEditing=null;navigate('tasks');$('#taskForm [name=title]')?.focus();return}
if(op==='tasks-more'){taskLimit+=TASK_CHUNK;updateTaskList()}
if(op==='tasks-first'){const list=$('#taskRows');if(list)list.scrollTop=0}
if(op==='tasks-today'){
 const items=taskItems();if(items.length){let i=items.findIndex(t=>t.date>=state.today);if(i<0)i=items.length-1;taskLimit=Math.max(taskLimit,Math.ceil((i+1)/TASK_CHUNK)*TASK_CHUNK);updateTaskList();const list=$('#taskRows'),row=[...list.querySelectorAll('[data-task-date]')].find(h=>h.dataset.taskDate===items[i].date);if(row)list.scrollTop+=row.getBoundingClientRect().top-list.getBoundingClientRect().top-44}
}
if(op==='complete'){const t=state.tasks.find(t=>t.id===id);await saveMutation('/api/tasks/'+id,'PATCH',{version:t.version,completed:!t.completed},t.completed?'미완료로 변경했습니다.':'할 일을 완료했습니다.')}
if(op==='edit-task'){taskEditing=id;navigate('tasks')}
if(op==='delete-task'){const t=state.tasks.find(t=>t.id===id);modal(`<h2>할 일을 삭제할까요?</h2><p>${E(t.title)}</p><div class="modal-choices"><button class="btn danger" data-op="confirm-delete" data-id="${id}" data-scope="one">이 날짜의 작업만 삭제</button>${t.series_id?`<button class="btn danger" data-op="confirm-delete" data-id="${id}" data-scope="future">이 날짜부터 이후 반복 모두 삭제</button><button class="btn danger" data-op="confirm-delete" data-id="${id}" data-scope="series">이 반복 작업 전체 삭제</button>`:''}<button class="btn" data-op="close-modal">취소</button></div>`)}
if(op==='confirm-delete'){const t=state.tasks.find(t=>t.id===id);await saveMutation(`/api/tasks/${id}?scope=${b.dataset.scope}&version=${t.version}`,'DELETE',undefined,out=>`${out.deleted}개의 작업을 삭제했습니다.`);closeModal()}
if(op==='preview-repeat'){if(!$('#taskForm').reportValidity())return;const out=await api('/api/tasks/preview','POST',taskBody());$('#repeatPreview').innerHTML=`<strong>총 ${out.count}개 생성 예정</strong><br>${out.dates.slice(0,6).map(E).join(' · ')}${out.count>6?' …':''}<br>마지막: ${E(out.dates[out.dates.length-1])} · 종료일까지 포함`}
if(op==='select-widget'){selectedWidget=b.dataset.wid;render()}
if(op==='size-widget'){const w=layoutDraft.widgets.find(w=>w.id===selectedWidget);[w.w,w.h]=b.dataset.size.split(',').map(Number);dirty=true;render()}
if(op==='remove-widget'){if(layoutDraft.widgets.length===1)throw Error('하나 이상의 위젯을 유지해 주세요.');layoutDraft.widgets=layoutDraft.widgets.filter(w=>w.id!==selectedWidget);selectedWidget=layoutDraft.widgets[0].id;dirty=true;render()}
if(op==='add-widget')addWidget(id);
if(op==='apply-widget-config'){const value=JSON.parse($('#widgetConfig').value);if(!value||Array.isArray(value)||typeof value!=='object')throw Error('설정은 JSON 객체여야 합니다.');layoutDraft.widgets.find(w=>w.id===selectedWidget).config=value;dirty=true;R.toast('설정 초안에 반영했습니다. 화면에 적용을 눌러 저장하세요.')}
if(op==='save-layout'){if($('#widgetConfig')){const value=JSON.parse($('#widgetConfig').value);if(!value||Array.isArray(value)||typeof value!=='object')throw Error('설정은 JSON 객체여야 합니다.');layoutDraft.widgets.find(w=>w.id===selectedWidget).config=value}validateLayout(layoutDraft);const result=await api('/api/layout','PUT',layoutDraft);layoutDraft=structuredClone(result);dirty=false;await refresh();R.toast('연결된 화면에 배치를 적용했습니다.')}
if(op==='discard-layout'){layoutDraft=structuredClone(state.layout);dirty=false;render()}
if(op==='reset-layout'){layoutDraft={columns:8,rows:6,version:state.layout.version,widgets:[{id:'clock',type:'clock',title:'지금',x:0,y:0,w:4,h:2,config:{}},{id:'weather',type:'weather',title:'날씨',x:4,y:0,w:4,h:2,config:{}},{id:'tasks',type:'todos',title:'할 일',x:0,y:2,w:4,h:4,config:{}},{id:'calendar',type:'calendar',title:'달력',x:4,y:2,w:4,h:4,config:{}}]};selectedWidget='clock';dirty=true;render()}
if(op==='reload-widgets'){const r=await api('/api/widgets/reload','POST');await refresh();R.toast(r.errors.length?`${r.errors.length}개 폴더의 오류를 확인하세요.`:'위젯 폴더를 다시 읽었습니다.')}
if(['remote','device-home','device-reload'].includes(op)){const action=op==='remote'?b.dataset.action:op==='device-home'?'home':'reload';let widget_id=op==='remote'?id:null;if(action==='expand'&&!state.layout.widgets.some(w=>w.id===widget_id)){const type=id==='tasks'?'todos':id;widget_id=state.layout.widgets.find(w=>w.type===type)?.id;if(!widget_id)throw Error('해당 종류의 위젯이 배치되어 있지 않습니다.')}const result=await api('/api/commands','POST',{action,widget_id,device_id:op==='remote'?null:id});R.toast(demo?'미리보기 화면을 제어했습니다.':result.delivered_connections?`${result.delivered_connections}개 연결에 명령을 보냈습니다.`:'연결된 화면이 없습니다. 기기 상태를 확인하세요.')}
if(op==='revoke-device')modal(`<h2>이 화면의 연결을 해제할까요?</h2><p>이 기기의 표시 권한을 취소합니다. 다시 사용하려면 새 연결 링크가 필요합니다.</p><div class="form-actions"><button class="btn danger" data-op="confirm-revoke" data-id="${id}">연결 해제</button><button class="btn" data-op="close-modal">취소</button></div>`);
if(op==='confirm-revoke'){await saveMutation('/api/devices/'+id,'DELETE',undefined,'기기 연결을 해제했습니다.');closeModal()}
if(op==='copy-pair'){const input=$('#pairLink');if(pairingState()==='active'&&input)await copyText(input.value,input);else syncPairPanel();}
if(op==='voice-to-llm'){const v=overview.voice.find(v=>v.id===id),draft=$(`[data-voice-transcript="${id}"]`);if(draft&&draft.value.trim()!==v.text.trim())throw Error('수정한 전사 내용을 먼저 전사 저장한 뒤 보내세요.');await window.RoomLLM.submitFromVoice(v,b);}
if(op==='voice-llm-history'){window.RoomLLM.filterVoice(id);navigate('llm');}
if(op==='voice-to-task'){const v=overview.voice.find(v=>v.id===id);taskEditing=null;filterDate=state.today;navigate('tasks');$('#taskForm [name=title]').value=v.text.slice(0,240);$('#taskForm [name=notes]').value='음성 수신함 원문: '+v.text;R.toast('원문을 작성창에 넣었습니다. 제목·날짜를 확인한 뒤 저장하세요.')}
if(op==='speech-enqueue'){if(demo){R.toast('실제 전사는 V35 서버에서 실행됩니다.');return}await saveMutation('/api/speech/enqueue/'+id,'POST',{},'V35 전사 대기열에 넣었습니다.')}
if(op==='speech-cancel')await saveMutation('/api/speech/jobs/'+id+'/cancel','POST',{},'전사 취소를 요청했습니다.');
if(op==='voice-reviewed')await saveMutation('/api/voice/'+id,'PATCH',{status:'reviewed'},'검토 완료로 표시했습니다. 작업이 자동 생성되지는 않습니다.');
if(op==='voice-transcript'){const text=$(`[data-voice-transcript="${id}"]`).value.trim();if(!text)throw Error('전사 내용을 입력하세요.');await saveMutation('/api/voice/'+id,'PATCH',{status:'pending_review',text},'전사 내용을 저장했습니다.')}
if(op==='voice-delete')modal(`<h2>수신 내용을 삭제할까요?</h2><p>원본 음성 파일이 있으면 함께 삭제합니다. 연결된 LLM 요청 스냅샷은 별도로 보관됩니다. LLM 화면에서 따로 삭제하세요. 되돌릴 수 없습니다.</p><div class="form-actions"><button class="btn danger" data-op="confirm-voice-delete" data-id="${id}">삭제</button><button class="btn" data-op="close-modal">취소</button></div>`);
if(op==='confirm-voice-delete'){await saveMutation('/api/voice/'+id,'DELETE',undefined,'수신 내용을 삭제했습니다.');closeModal()}
if(op==='integration'){const out=await api('/api/admin/integration');modal(`<h2>음성 입력 연동</h2><p>입력 전용 키는 음성 수신 API에만 사용됩니다. 외부 음성 기기의 실제 연결은 별도 어댑터가 필요합니다.</p><label style="margin-top:18px">입력 전용 Bearer 키<input readonly value="${E(out.ingest_token)}" id="integrationKey"></label><pre style="margin-top:15px">POST /api/voice/text\nPOST /api/voice/upload\nAuthorization: Bearer &lt;INGEST_KEY&gt;</pre><p style="margin-top:15px">STT: ${out.transcription_enabled?'V35 로컬 엔진 준비됨':'설치 또는 활성화 필요'} · 자동 실행: 꺼짐</p><div class="form-actions"><button class="btn" data-op="copy-key">키 복사</button><button class="btn primary" data-op="close-modal">닫기</button></div>`)}
if(op==='copy-key')await copyText($('#integrationKey').value,$('#integrationKey'));
if(op==='weather-refresh'){await api('/api/weather/refresh','POST');await refresh();R.toast(demo?'미리보기 날씨는 고정된 예시입니다.':state.weather?.error||state.weather_error||'날씨 갱신을 요청했습니다.')}
if(op==='export-json'&&demo){e.preventDefault();downloadJSON()}
if(op==='demo-download'){e.preventDefault();R.toast('미리보기에는 실제 서버 파일이 없습니다. 실행 패키지에서 사용할 수 있습니다.')}
if(op==='close-modal')closeModal();
if(op==='logout'){await api('/api/auth/logout','POST');if(!demo)showLogin()}
}catch(err){R.toast(err.message)}});
$('#modal').addEventListener('click',e=>{if(e.target===$('#modal'))closeModal()});document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
$('#loginForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/auth/login','POST',{token:new FormData(e.target).get('token')});e.target.reset();$('#loginError').textContent='';await start()}catch(err){$('#loginError').textContent=err.message}};
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
async function start(){try{if(!demo)await R.api('/api/auth/me');$('#login').classList.add('hidden');$('#managerApp').classList.remove('hidden');if(demo){$('#demoNotice').classList.remove('hidden');$('#serverLabel').textContent='예시 데이터 · 로컬 미리보기';$('#managerStatus').innerHTML='<i class="status-dot"></i> 데모 모드';$('#openClient').onclick=e=>{e.preventDefault();if(window.ROOM_CLIENT_PREVIEW){const url=URL.createObjectURL(new Blob([window.ROOM_CLIENT_PREVIEW],{type:'text/html'}));window.open(url,'_blank');setTimeout(()=>URL.revokeObjectURL(url),60000)}}}tab=tabs[location.hash.slice(1)]?location.hash.slice(1):'overview';await refresh();if(!demo){socket?.close();socket=R.connect('manager',msg=>{if(['invalidate','device_ack'].includes(msg.type)){const editing=['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)||tab==='layout';refresh(!editing)}if(msg.type==='revoked')showLogin()},ok=>{$('#managerStatus').innerHTML=`<i class="status-dot ${ok?'':'off'}"></i> ${ok?'실시간 연결':'재연결 중'}`})}}catch(e){if(e.status===401)showLogin();else R.toast(e.message)}}
setInterval(syncPairPanel,1000);
setInterval(()=>{if(!demo&&state&&!$('#managerApp').classList.contains('hidden'))refresh(tab==='overview'||tab==='devices')},30000);window.RoomManager={navigate,getState:()=>state,getOverview:()=>overview,getTab:()=>tab,refresh};start();
})();
