/* Production UI logic in a Node VM with synthetic DOM/API/timers.
 * No browser rendering, sockets, microphone, real server, device, or paid API. */
'use strict';
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..');

function harness(profile='odyssey_openai'){
 const elements=[],calls=[],intervals=[],notices=[],events={};let serial=0;
 function matches(el,selector){
  selector=selector.trim();const parts=selector.split(/\s+(?=[.#\[])/);
  if(parts.length>1){const child=parts.pop();return matches(el,child)&&parts.every(s=>{let n=el.owner;while(n){if(matches(n,s))return true;n=n.owner;}return false;});}
  const tag=selector.match(/^[a-z]+/i);if(tag&&el.tagName!==tag[0].toUpperCase())return false;
  const id=selector.match(/#([\w-]+)/);if(id&&el.id!==id[1])return false;
  const cls=selector.match(/\.([\w-]+)/);if(cls&&!el.classes.has(cls[1]))return false;
  for(const a of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)){
   const value=el.attrs[a[1]];if(value===undefined||(a[2]!==undefined&&value!==a[2]))return false;
  }
  return true;
 }
 class Element{
  constructor(tag='div',owner=null){this.tagName=tag.toUpperCase();this.owner=owner;this.children=[];this.listeners={};this.dataset={};this.attrs={};this.classes=new Set();this.style={};this.hidden=false;this.disabled=false;this.value='';this.textContent='';this.scrollTop=0;this.isConnected=true;this._html='';elements.push(this);this.classList={add:v=>this.classes.add(v),remove:v=>this.classes.delete(v),contains:v=>this.classes.has(v),toggle:(v,on)=>{if(on??!this.classes.has(v))this.classes.add(v);else this.classes.delete(v);}};}
  set innerHTML(value){for(const child of this.children)child.remove();this.children=[];this._html=String(value);
   for(const m of this._html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)){
    const child=new Element(m[1],this);this.children.push(child);
    for(const a of m[2].matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)')?/g)){
     const name=a[1],val=a[2]??a[3]??'';child.attrs[name]=val;
     if(name==='id')child.id=val;
     if(name==='class'){child.className=val;child.classes=new Set(val.split(/\s+/));}
     if(name.startsWith('data-'))child.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=val;
     if(name==='disabled')child.disabled=true;if(name==='hidden')child.hidden=true;
    }
   }
  }
  get innerHTML(){return this._html;}
  querySelectorAll(selector){const candidates=elements.filter(e=>e.isConnected&&e!==this);return candidates.filter(e=>{let n=e.owner,inside=false;while(n){if(n===this){inside=true;break;}n=n.owner;}return inside&&selector.split(',').some(s=>matches(e,s));});}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  closest(selector){let n=this;while(n){if(selector.split(',').some(s=>matches(n,s)))return n;n=n.owner;}return null;}
  addEventListener(name,fn){this.listeners[name]=fn;}
  remove(){this.isConnected=false;for(const child of this.children)child.remove();}
  append(...children){for(const child of children){child.owner=this;this.children.push(child);}}
  replaceChildren(...children){for(const child of this.children)child.remove();this.children=[];this.append(...children);}
  hasAttribute(name){return name in this.attrs;}
 }
 const body=new Element('body'),root=new Element('div',body);root.id='llmRoot';body.children.push(root);
 const document={body,hidden:false,activeElement:null,createElement:tag=>new Element(tag),
  querySelector:selector=>body.querySelector(selector),querySelectorAll:selector=>body.querySelectorAll(selector),
  getElementById:id=>elements.find(e=>e.id===id&&e.isConnected)||null,addEventListener:(name,fn)=>events[name]=fn};
 const history={id:'retired-request',status:'prepared',created_at:'2026-01-01T00:00:00Z',source_text:'합성 전사',source_preview:'합성 전사',config_json:{model:'retired-model'},source_meta:{source:'synthetic'},request_payload:{messages:[]},endpoint:'http://127.0.0.1:8179/v1/chat/completions',metrics:{},response_json:null};
 const env={profile,configFailure:false,agentStatusFailure:false,history:false,cloud_status:'ready',state:null};
 const response=(data,status=200)=>({ok:status<400,status,json:async()=>JSON.parse(JSON.stringify(data))});
 const context={console,Date,Math,JSON,Intl,Map,Set,Promise,Error,AbortController,URLSearchParams,Blob,FormData,URL,
  crypto:{randomUUID:()=>`synthetic-${++serial}`},document,
  setTimeout:()=>++serial,clearTimeout(){},setInterval:(fn,delay)=>{intervals.push({fn,delay});return intervals.length;},clearInterval(){},
  queueMicrotask,MutationObserver:class{observe(){}disconnect(){}},CSS:{escape:x=>x},addEventListener:(n,f)=>events[n]=f,
  RoomManager:{getTab:()=> 'llm',getState:()=>({settings:{timezone:'Asia/Seoul'}}),navigate(){},refresh:async()=>{}},
  fetch:async(url,options={})=>{
   const method=options.method||'GET',payload=options.body?JSON.parse(options.body):undefined;calls.push({url,method,payload});
   if(url==='/api/llm/config')return response({runtime_profile:env.profile,config:{enabled:true,base_url:'http://127.0.0.1:8179',model:'retired-model'}},env.configFailure?503:200);
   if(url.startsWith('/api/llm/requests?'))return response({items:env.history?[history]:[],total:env.history?1:0});
   if(url==='/api/llm/requests/retired-request')return response(history);
   if(url==='/api/agent/status')return response({runtime_profile:env.profile,cloud_status:env.cloud_status,agent_enabled:true,cloud_enabled:true,audio_cloud_enabled:true,adapters:['todo','alarm'],tiers:{tier1_stt:'synthetic-transcribe',tier2_fast:'synthetic-fast (reasoning_effort=high)',tier3_deep:'synthetic-deep (reasoning_effort=high)'},budget:{}},env.agentStatusFailure?503:200);
   if(url.startsWith('/api/agent/runs?'))return response({items:[]});
   if(method==='POST'&&url==='/api/agent/command')return response({id:'agent-run',status:'succeeded'});
   if(method==='POST'&&url==='/api/llm/requests')return response(history);
   if(url==='/healthz')return response({status:'ok'});
   if(url==='/api/ai/status'||url==='/api/ai/probe')return response(env.state);
   throw Error(`Unexpected ${method} ${url}`);
  }};
 context.window=context;context.globalThis=context;vm.createContext(context);
 const load=file=>vm.runInContext(fs.readFileSync(path.join(ROOT,file),'utf8'),context,{filename:file});
 load('web/shared.js');context.Room.toast=m=>notices.push(m);
 return {context,env,root,calls,intervals,notices,load,Element,node:id=>document.getElementById(id),query:s=>document.querySelector(s)};
}
async function settle(){for(let i=0;i<80;i++)await Promise.resolve();}

async function main(){
 const checks=[];const check=(name,condition)=>{assert.ok(condition,name);checks.push({name,passed:true});};
 const h=harness();h.load('web/manager-llm.js');
 const voice={id:'voice-1',text:'오늘 할 일 확인',text_sha256:'a'.repeat(64)},button={disabled:false,isConnected:true};
 await h.context.RoomLLM.submitFromVoice(voice,button);
 check('Voice import resolves backend profile before the LLM tab is mounted',h.calls[0].url==='/api/llm/config'&&h.calls[0].method==='GET');
 check('Odyssey voice import sends only to Personal Agent',h.calls.filter(c=>c.method==='POST').every(c=>c.url==='/api/agent/command')&&h.calls.some(c=>c.url==='/api/agent/command'));
 h.context.RoomLLM.setMode('legacy');
 check('Retired mode selection cannot override PC-free routing',h.context.RoomLLM.isPCFree()&&h.context.RoomLLM.getMode()==='personal_agent');
 check('Cloud API credentials are never requested or submitted by the UI',h.calls.every(c=>c.url.startsWith('/api/')&&!JSON.stringify(c.payload||{}).includes('api_key')));

 const blocked=harness();blocked.env.configFailure=true;blocked.load('web/manager-llm.js');
 await assert.rejects(()=>blocked.context.RoomLLM.submitFromVoice(voice,{...button}));
 check('Profile lookup failure fails closed without legacy submission',blocked.calls.every(c=>c.method==='GET'));

 const legacy=harness('legacy');legacy.load('web/manager-llm.js');await legacy.context.RoomLLM.submitFromVoice(voice,{...button});
 check('Legacy profile retains its existing request endpoint',legacy.calls.some(c=>c.method==='POST'&&c.url==='/api/llm/requests')&&!legacy.calls.some(c=>c.url==='/api/agent/command'));

 const mounted=harness();mounted.env.history=true;mounted.env.agentStatusFailure=true;mounted.load('web/manager-llm.js');mounted.context.RoomLLM.mount(mounted.root);await settle();
 check('Config profile locks retired history even if agent status is unavailable',mounted.context.RoomLLM.isPCFree()&&mounted.root.dataset.runtimeProfile==='odyssey_openai');
 check('PC-free selector and retired configuration/probe controls are disabled',mounted.node('assistantMode').disabled&&['probe','settings'].every(op=>{const b=mounted.query(`[data-llm="${op}"]`);return b.hidden&&b.disabled;}));
 check('Historical send/retry/delete/cancel controls are read-only, export remains available',['send','retry','delete','cancel'].every(op=>{const b=mounted.node('llmDetail').querySelector(`[data-llm="${op}"]`);return !b||(b.hidden&&b.disabled);})&&!mounted.node('llmDetail').querySelector('[data-llm="export"]').disabled);
 const beforeDialog=mounted.calls.length;mounted.load('web/manager-dialog.js');await settle();
 check('Retired dialog add-on sees profile before reading or offering a continuation',mounted.calls.length===beforeDialog);
 const staleButton=mounted.node('llmDetail').querySelector('[data-llm="send"]');staleButton.disabled=false;await mounted.root.listeners.click({target:staleButton});
 check('Delegated handler also rejects a stale retired action',!mounted.calls.some(c=>c.method!=='GET')&&mounted.notices.some(t=>t.includes('기존 모델')));
 mounted.env.agentStatusFailure=false;mounted.env.cloud_status='budget_hard_cap_reached';await mounted.context.RoomLLM.refresh();
 check('Budget state distinguishes cloud block from absent PC',mounted.node('agentStatusBadge').textContent.includes('예산 한도')&&mounted.node('agentRuntimeNotice').textContent.includes('fallback은 차단'));
 check('Cloud catalog uses actual agent adapters and configured model names',mounted.node('assistantCatalog').innerHTML.includes('alarm')&&!mounted.node('assistantCatalog').innerHTML.includes('미연결')&&mounted.node('agentModelSettings').textContent.includes('synthetic-fast'));

 const runtime=harness();for(const id of ['architecture','probe','scopeNote','speech','llm','core','message']){const n=new runtime.Element();n.id=id;runtime.root.append(n);}
 runtime.env.state={runtime_profile:'odyssey_openai',speech:{enabled:true,state:'configured',backend:'openai',configured_model:'<img src=x onerror=BAD()>',preprocessing:'Odyssey FFmpeg',identity_note:'local configuration only'},llm:{enabled:true,state:'configured',backend:'openai',configured_model:'synthetic-fast',configured_deep_model:'synthetic-deep',reasoning_effort:'high',identity_note:'local configuration only'}};
 runtime.load('web/runtime.js');await settle();
 check('Runtime initial load and polling issue only same-origin read requests',runtime.calls.every(c=>c.method==='GET'&&c.url.startsWith('/')));
 check('Runtime clearly separates configured from provider-verified',runtime.node('speech').children.some(n=>n.textContent.includes('실제 API 호출 미검증'))&&runtime.node('scopeNote').textContent.includes('OpenAI에 접속하지 않습니다'));
 check('Untrusted configured model is inserted as text, never HTML',runtime.node('speech').children.some(n=>n.textContent.startsWith('<img'))&&runtime.node('speech').innerHTML==='');
 await runtime.node('probe').listeners.click();await settle();
 check('Explicit Odyssey refresh has no inference endpoint and says no paid call',runtime.calls.filter(c=>c.method==='POST').length===1&&runtime.calls.find(c=>c.method==='POST').url==='/api/ai/probe'&&runtime.node('message').textContent.includes('유료 추론은 실행하지 않았습니다'));
 runtime.env.state.speech.state='api_key_missing';runtime.env.state.llm.state='budget_exhausted';await runtime.intervals[0].fn();await settle();
 check('Runtime polling displays missing key and budget gates without re-probing',runtime.node('speech').children.some(n=>n.textContent.includes('API 키 미설정'))&&runtime.node('llm').children.some(n=>n.textContent.includes('예산 한도'))&&runtime.calls.filter(c=>c.method==='POST').length===1);
 const output={passed:checks.length,checks,scope:'Production JS in Node VM; synthetic DOM/API/timers. No browser rendering, HTTP/CSP, live API, microphone, server, or physical device.'};
 const reports=path.join(ROOT,'artifacts/test-results');fs.mkdirSync(reports,{recursive:true});fs.writeFileSync(path.join(reports,'odyssey-ui-regression.json'),JSON.stringify(output,null,2));
 console.log(JSON.stringify({passed:checks.length,scope:output.scope}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
