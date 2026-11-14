/* Production speech JS in a Node VM with synthetic DOM/API/timers. No sockets,
   microphone, cloud calls, server, or physical-device claims. */
'use strict';
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..');

function harness(){
 const nodes=new Map(),timers=new Map(),storage=new Map([['room-speech-job','synthetic-job']]),events={},calls=[];
 let serial=0,context;
 class Element{
  constructor(tag='div'){this.tagName=tag.toUpperCase();this._html='';this.textContent='';this.children=new Set();this.listeners={};this.classList={add(){},remove(){}};}
  set innerHTML(value){
   for(const id of this.children)nodes.delete(id);this.children.clear();this._html=String(value);
   for(const match of this._html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)){
    const element=new Element(match[1]);element.id=match[2];nodes.set(element.id,element);this.children.add(element.id);
   }
  }
  get innerHTML(){return this._html;}
  setAttribute(){}
  append(element){if(element.id)nodes.set(element.id,element);}
  addEventListener(name,listener){this.listeners[name]=listener;}
  querySelectorAll(){return [...this.children].map(id=>nodes.get(id)).filter(Boolean);}
  getClientRects(){return [{}];}
  focus(){context.document.activeElement=this;}
 }
 const env={cfg:{ready:true,backend:'openai',agent_enabled:true,model:'synthetic-transcribe',max_seconds:30},
  job:{id:'synthetic-job',status:'queued',text:'',agent_run:null},failure:null};
 context={console,Date,Math,JSON,Map,Set,Promise,Error,AbortController,Uint8Array,
  isSecureContext:true,navigator:{mediaDevices:{getUserMedia:async()=>{throw Error('Microphone forbidden');}}},
  MediaRecorder:class{},crypto:{randomUUID:()=>`synthetic-${++serial}`},
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  setTimeout:(fn,delay)=>{const id=++serial;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
  setInterval:()=>0,clearInterval:()=>{},
  document:{hidden:false,body:new Element('body'),activeElement:null,querySelector:selector=>nodes.get(selector.replace(/^#/,''))||null,
   querySelectorAll:()=>[],createElement:tag=>new Element(tag),addEventListener:(name,fn)=>events[name]=fn},
  addEventListener:(name,fn)=>events[name]=fn,
  fetch:async(url,options={})=>{
   const method=options.method||'GET';calls.push({url,method});assert.equal(method,'GET','lifecycle may never resubmit or approve');
   if(url==='/api/speech/status')return response(env.cfg);
   assert.equal(url,'/api/speech/jobs/synthetic-job');
   if(env.failure!==null){const failure=env.failure;env.failure=null;if(failure===0)throw Error('synthetic offline');return response({detail:'Synthetic request denied'},failure);}
   if(env.deferNext){env.deferNext=false;const snapshot=JSON.parse(JSON.stringify(env.job));return await new Promise(resolve=>env.releaseDeferred=()=>resolve(response(snapshot)));}
   return response(env.job);
  }};
 context.window=context;context.top=context;context.self=context;
 vm.createContext(context);
 for(const file of ['web/shared.js','web/speech.js'])vm.runInContext(fs.readFileSync(path.join(ROOT,file),'utf8'),context);
 function response(data,status=200){return {ok:status<400,status,json:async()=>JSON.parse(JSON.stringify(data))};}
 function polls(){return [...timers].filter(([,timer])=>timer.delay===1500||timer.delay===4000);}
 async function tick(){const [id,timer]=polls()[0]||[];assert.ok(timer,'expected polling');timers.delete(id);await timer.fn();}
 return {context,env,nodes,events,calls,storage,polls,tick,html:()=>nodes.get('speechBody').innerHTML,
  message:()=>nodes.get('speechMessage').textContent,privacy:()=>nodes.get('speechPrivacy').textContent};
}

async function main(){
 const checks=[];
 const check=(name,condition)=>{assert.ok(condition,name);checks.push({name,passed:true});};
 const h=harness();await h.context.RoomSpeech.open();
 check('Queued STT polls and backend label states OpenAI',h.polls().length===1&&h.privacy().includes('OpenAI 클라우드'));
 h.env.job={...h.env.job,status:'succeeded',text:'오늘 할 일 확인',agent_run:{status:'pending'}};await h.tick();
 check('STT success keeps polling while agent pending',h.polls().length===1&&h.html().includes('개인 비서 요청을 준비'));
 h.env.job.agent_run={id:'synthetic-run',status:'running',summary_text:''};await h.tick();
 check('Agent running remains distinct and polls',h.polls().length===1&&h.html().includes('요청을 처리하고'));
 h.env.job.agent_run={id:'synthetic-run',status:'awaiting_confirmation',summary_text:'Proposed change',awaiting_admin_confirmation:true};await h.tick();
 check('Awaiting manager confirmation polls but exposes no approval button',h.polls().length===1&&h.html().includes('이 화면에서는 승인하지 않습니다')&&!/id="[^"]*(?:Confirm|Approve)/.test(h.html())&&!h.nodes.has('speechCancelJob'));
 h.context.RoomSpeech.close();
 check('Close clears poll, preserves existing job',h.polls().length===0&&h.storage.get('room-speech-job')==='synthetic-job');
 await h.context.RoomSpeech.open();
 check('Reopen resumes existing confirmation state using GET',h.polls().length===1&&h.html().includes('관리자 확인을 기다리고'));
 h.env.failure=0;await h.tick();
 check('Network loss preserves job and schedules only status lookup',h.polls().length===1&&h.polls()[0][1].delay===4000&&h.message().includes('연결을 기다리고'));
 h.env.job.agent_run={id:'synthetic-run',status:'succeeded',summary_text:'<img src=x onerror=alert(1)>\n완료',redacted:false};await h.tick();
 check('Completed agent stops polling and escapes summary HTML',h.polls().length===0&&h.html().includes('&lt;img')&&!h.html().includes('<img src=x'));
 h.context.RoomSpeech.close();h.env.job.agent_run={status:'running',summary_text:'OLD_RUNNING'};await h.context.RoomSpeech.open();
 h.env.deferNext=true;const olderPoll=h.tick();assert.ok(h.env.releaseDeferred);
 h.env.job.agent_run={status:'succeeded',summary_text:'NEW_COMPLETED'};h.events.online();
 for(let i=0;i<12;i++)await Promise.resolve();
 h.env.releaseDeferred();await olderPoll;
 check('An older in-flight GET cannot rewind a newer completed result',h.polls().length===0&&h.html().includes('NEW_COMPLETED')&&!h.html().includes('OLD_RUNNING'));
 h.context.RoomSpeech.close();h.env.job.agent_run={status:'interrupted',summary_text:'관리자 기록에서 확인',redacted:true};await h.context.RoomSpeech.open();
 check('Interrupted/private result shows explicit no-retry and manager notice',h.polls().length===0&&h.html().includes('자동 재실행하지 않습니다')&&h.html().includes('공유되지 않은 내용')&&!h.nodes.has('speechRetryJob'));
 h.context.RoomSpeech.close();h.env.cfg.backend='remote_http';h.env.job.agent_run=null;await h.context.RoomSpeech.open();
 check('Legacy STT-only response stops and labels remote backend truthfully',h.polls().length===0&&!h.nodes.has('speechAgentResult')&&h.privacy().includes('원격 Whisper')&&!h.html().includes('V35'));
 h.context.RoomSpeech.close();h.env.job.agent_run={status:'pending'};await h.context.RoomSpeech.open();h.env.failure=401;await h.tick();
 check('Unauthorized session stops polling without auto-repair',h.polls().length===0&&h.message().includes('만료'));
 check('All lifecycle and reconnect requests were GET only',h.calls.every(call=>call.method==='GET'));
 const output={passed:checks.length,checks,scope:'Production JS; Node VM + synthetic DOM/API/timers. No browser rendering, paid API, microphone, live backend, or real device.'};
 const reports=path.join(ROOT,'artifacts/test-results');fs.mkdirSync(reports,{recursive:true});fs.writeFileSync(path.join(reports,'agent-speech-transport.json'),JSON.stringify(output,null,2));
 console.log(JSON.stringify({passed:checks.length,scope:output.scope}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
