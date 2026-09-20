/* Preview fixture only. Never loaded by production manager.html. No model calls. */
(()=>{
 const clone=x=>structuredClone(x),now=()=>new Date().toISOString();
 let cfg={enabled:false,base_url:'http://127.0.0.1:8090/v1',model:'Qwen3-0.6B-Q5_K_M.gguf',system_prompt:'[예시] Room Hub의 입력을 검토하세요. 실제 작업을 실행하지 않습니다.',include_time_context:true,max_tokens:256,temperature:.2,timeout_seconds:180,non_thinking:true};
 let rows=[];let seq=0;const make=(text,status='prepared')=>{const time=now(),id='demo-llm-'+(++seq),payload={model:cfg.model,messages:[{role:'system',content:cfg.system_prompt+'\n[예시 기준 시각] 2026-09-20T09:00:00+09:00'},{role:'user',content:text}],stream:false,max_tokens:cfg.max_tokens,temperature:cfg.temperature,chat_template_kwargs:{enable_thinking:false}};let r={id,voice_id:'voice-demo',source_voice_id:'voice-demo',parent_id:null,source_text:text,source_preview:text,source_meta:{source:'예시 음성 입력',kind:'audio'},config_json:clone(cfg),request_body:JSON.stringify(payload),request_payload:payload,request_sha256:'DEMO_ONLY_NOT_REAL_HASH',endpoint:cfg.base_url+'/chat/completions',status,created_at:time,updated_at:time,started_at:null,finished_at:null,request_seconds:null,wait_seconds:null,dispatch_attempted:false,response_json:null,response_raw:null,error:'',error_code:null};if(status==='succeeded'){const output='[예시 응답 · 모델 실행 아님]\n내일 오후 3시에 택배를 보내려는 요청입니다. 실제 할 일을 추가하지 않았습니다.';r={...r,dispatch_attempted:true,started_at:time,finished_at:time,request_seconds:2.48,wait_seconds:.02,response_json:{output,response_model:'Qwen3-0.6B · 예시',finish_reason:'stop',warnings:['화면 확인용 합성 응답과 통계입니다. 실제 LLM 속도가 아닙니다.'],metrics:{request_seconds:2.48,output_tokens:48,output_tokens_source:'usage.completion_tokens (예시)',prompt_tokens:132,generation_seconds:2,generation_tps:24,generation_tps_source:'backend fixture',prompt_seconds:.42,end_to_end_tps:48/2.48}},response_raw:JSON.stringify({choices:[{message:{content:output}}],usage:{prompt_tokens:132,completion_tokens:48},timings:{predicted_n:48,predicted_ms:2000,predicted_per_second:24}},null,2)}}return r};

 function assistant(r,mode='auto'){
  if(mode==='legacy')return r;
  const text=r.source_text,day=window.ROOM_DEMO.today,normalized=text.replace(/\s+/g,' ').replace(/[?!.]+$/,'').trim();
  const create=mode!=='chat'&&/할\s*일에\s*(.+?)\s*추가해/.exec(normalized),list=mode!=='chat'&&/남은\s*할\s*일/.test(normalized);
  const state=create?'awaiting_confirmation':'succeeded',route=create||list?'rule':'chat';
  const a={mode,protocol:'room-assistant-1',raw:text,normalized,normalization_notes:text===normalized?[]:['예시 문장부호 정리'],route,origin:route==='rule'?'server':'llm',state,reference_at:window.ROOM_DEMO.server_time,timezone:'Asia/Seoul',calls:[],validation:create?'confirmation_required':list?'server_read':'chat_only',proposal:create?{intent:'todo.create',date_ref:'오늘',title:create[1],time:null,status:'all',scope:'one'}:list?{intent:'todo.list',date_ref:'오늘',status:'pending',scope:'one',title:null,time:null}:null};
  let output;
  if(create){a.preview={intent:'todo.create',task:{title:create[1],date:day,time:null},targets:[],existing_same:0,expires_at:Date.now()/1000+600};a.preview_sha256='0'.repeat(64);output=`[예시] ${day} · ${create[1]}\n할 일 1개를 추가합니다. 아직 변경하지 않았습니다. 아래 확인 버튼을 시험해 보세요.`;}
  else if(list){const tasks=window.ROOM_DEMO.tasks.filter(t=>t.date===day&&!t.completed);a.tool_result={count:tasks.length,items:tasks};output=`[예시 DB] 오늘 남은 할 일 ${tasks.length}개\n`+tasks.map(t=>t.title).join('\n');}
  else output='[모의 응답 · 실제 모델 아님] 김치볶음밥이나 카레를 추천합니다. 간단하게 만들 수 있습니다.';
  a.final_text=output;
  return {...r,status:state,assistant:a,started_at:now(),finished_at:now(),dispatch_attempted:route==='chat',endpoint:route==='chat'?r.endpoint:'local://room-hub',request_seconds:route==='chat'?2.1:.004,response_json:{output,output_source:a.origin,metrics:route==='chat'?{output_tokens:30,prompt_tokens:60,generation_tps:20,generation_tps_source:'합성 예시'}:{},warnings:['독립 예시 화면입니다. 서버와 실제 모델을 사용하지 않습니다.']}};
 }
 rows.push(make('이번 주 토요일까지 매일 운동하기를 추가해 줘.'));
 rows.unshift(assistant(make('저녁메뉴 추천해 줘.'),'chat'));
 rows.unshift(assistant(make('오늘 남은 할 일 확인해줘')));
 rows.unshift(assistant(make('오늘 할 일에 택배 보내기 추가해?')));
 function summary(r){return {...r,model:r.config_json.model,metrics:r.response_json?.metrics||{},source_preview:r.source_text,output_preview:r.response_json?.output||''}}
 window.ROOM_LLM_DEMO={async api(path,method='GET',body){
  const u=new URL(path,'http://demo'),p=u.pathname;
  if(p==='/api/llm/config'){if(method==='PUT')cfg=clone(body);return {config:clone(cfg),counts:{prepared:rows.filter(r=>r.status==='prepared').length,succeeded:rows.filter(r=>r.status==='succeeded').length},last_probe:null,max_pending:4,max_history:1000,streaming:false,auto_execute:false}}
  if(p==='/api/llm/probe')return {ok:false,reachable:false,checked_at:now(),models:[],message:'독립 미리보기입니다. 실제 연결 확인은 서버에서 하세요.'};
  if(p==='/api/llm/requests'){
   if(method==='POST'){const voice=window.RoomManager.getOverview().voice.find(v=>v.id===body.voice_id),r=assistant(make(voice.text),body.mode||'legacy');rows.unshift(r);voice.llm_count=(voice.llm_count||0)+1;return clone(r)}
   let items=rows.filter(r=>(!u.searchParams.get('status')||r.status===u.searchParams.get('status'))&&(!u.searchParams.get('query')||r.source_text.includes(u.searchParams.get('query')))&&(!u.searchParams.get('voice_id')||r.source_voice_id===u.searchParams.get('voice_id')));const offset=Number(u.searchParams.get('offset')||0),limit=Number(u.searchParams.get('limit')||30);return {items:items.slice(offset,offset+limit).map(summary),total:items.length,offset,limit};
  }
  if(p.startsWith('/api/assistant/')&&p.endsWith('/confirm')){const id=p.split('/')[3],r=rows.find(x=>x.id===id);if(!r)throw Error('예시 기록 없음');if(r.assistant?.validation!=='executed'){r.status='succeeded';r.assistant.state='succeeded';r.assistant.validation='executed';r.assistant.tool_result={count:1,demo:true};r.assistant.final_text='[예시 확인 완료] '+r.assistant.preview.task.title+' — 실제 서버 작업은 변경하지 않았습니다.';r.response_json.output=r.assistant.final_text;r.updated_at=now();}return clone(r);}
  const id=p.split('/')[4],r=rows.find(x=>x.id===id);if(!r)throw Error('예시 기록이 없습니다.');
  if(p.endsWith('/retry')){const j=r.assistant?.validation==='executed'?{...clone(r),id:'demo-llm-'+(++seq),source_text:r.source_text}:assistant(make(r.source_text),body?.mode||r.assistant?.mode||'legacy');j.parent_id=r.id;rows.unshift(j);return clone(j)}
  if(p.endsWith('/send'))throw Error('예시 화면에서는 실제 LLM에 전송하지 않습니다.');
  if(p.endsWith('/cancel')){r.status='cancelled';r.updated_at=now();if(r.assistant){r.assistant.state='cancelled';r.assistant.final_text='예시 요청을 취소했습니다.';r.response_json.output=r.assistant.final_text;}return clone(r)}
  if(method==='DELETE'){rows=rows.filter(x=>x.id!==id);return {ok:true}}
  return clone(r);
 }};
})();
