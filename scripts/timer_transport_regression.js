/* Deterministic production-transport regressions. No server/model/device access. */
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
let mono=0,wall=1790000000000,seq=0,requests=[],updates=[];
const listeners={},calls=[];
const DateMock=class extends Date{static now(){return wall;}};
const context={Date:DateMock,performance:{now:()=>mono},console,Map,Set,JSON,Math,Promise,Error,AbortController,
 setInterval:()=>0,setTimeout:()=>0,clearTimeout:()=>{},
 document:{hidden:false,addEventListener:(n,f)=>listeners[n]=f},
 fetch:(path,opt)=>new Promise((resolve,reject)=>{requests.push({path,opt,resolve,reject});calls.push({path,opt});})};
context.window=context;context.top=context;context.self=context;
context.addEventListener=(n,f)=>listeners[n]=f;context.crypto={randomUUID:()=>`key-${++seq}`};
context.RoomDisplay={getState:()=>({capabilities:{timer_control:true}}),updateTimers(data){updates.push(data);context.RoomTimers.sync(data);},refresh:()=>{}};
vm.createContext(context);vm.runInContext(fs.readFileSync('web/timers.js','utf8'),context);
const R=context.RoomTimers;
const snap=(revision,epoch=wall)=>({revision,server_time:new Date(epoch).toISOString(),items:[]});
function respond(request,data,status=200){request.resolve({ok:status<400,status,json:async()=>data});}
async function main(){
 const original=snap(1);R.sync(original);const initial=R.now();
 mono+=5000;wall+=5000;R.sync(original);assert.equal(R.now(),initial+5000,'cached snapshot must not reset elapsed time');
 R.offline();mono+=1000;wall+=1000;R.sync(original,true);assert.equal(R.now(),initial+6000,'reconnect must not reanchor cached time');
 R.sync(snap(0));assert.equal(R.now(),initial+6000,'older revision ignored');
 R.sync({...snap(5),server_time:'invalid'});assert.equal(R.now(),initial+6000,'invalid time ignored');
 assert.equal(R.needsSync(),false);
 // An older poll remains pending when A starts. A's response must update immediately.
 const poll=R.refresh(),oldPoll=requests.shift();
 const pa=R.mutate('timer-a','start',{duration_seconds:240});const a=requests.shift();
 assert.equal(JSON.parse(a.opt.body).widget_id,'timer-a');
 const pb=R.mutate('timer-b','start',{duration_seconds:90});const b=requests.shift();
 assert.equal(JSON.parse(b.opt.body).widget_id,'timer-b');
 assert.notEqual(JSON.parse(a.opt.body).request_id,JSON.parse(b.opt.body).request_id);
 await assert.rejects(()=>R.mutate('timer-a','start',{duration_seconds:1}),/처리 중/);
 respond(a,{changed:true,snapshot:snap(2)});await pa;
 assert.equal(updates.length,1,'mutation snapshot not blocked by in-flight GET');
 respond(b,{changed:true,snapshot:snap(3)});await pb;
 const after=R.now();respond(oldPoll,original);await poll;assert.equal(R.now(),after,'old poll cannot rewind timer anchor');
 // An ambiguous transport failure retains only that widget's request key.
 const p1=R.mutate('timer-a','stop',{version:1},'run-a');const r1=requests.shift();r1.reject(Error('network lost'));await assert.rejects(()=>p1,/network lost/);
 const p2=R.mutate('timer-a','stop',{version:1},'run-a');const r2=requests.shift();assert.equal(JSON.parse(r1.opt.body).request_id,JSON.parse(r2.opt.body).request_id);
 respond(r2,{duplicate:true,snapshot:snap(4)});await p2;
 // A definitive rejected operation must not become an everlasting stale request.
 const p3=R.mutate('timer-a','start',{duration_seconds:1});const r3=requests.shift();respond(r3,{detail:'busy'},409);await assert.rejects(()=>p3,/busy/);
 const p4=R.mutate('timer-a','start',{duration_seconds:1});const r4=requests.shift();assert.notEqual(JSON.parse(r3.opt.body).request_id,JSON.parse(r4.opt.body).request_id);respond(r4,{snapshot:snap(5)});await p4;
 // Wall-clock changes are not blindly converted into countdown time.
 wall+=90000;assert.equal(R.needsSync(),true);assert.equal(R.now(),after);
 const rs=requests.shift();respond(rs,snap(6));await R.refresh();assert.equal(R.needsSync(),false);
 // Revocation/clear invalidates outstanding replies.
 const pending=R.refresh(),rp=requests.shift(),before=updates.length;R.clear();respond(rp,snap(7));await pending;assert.equal(updates.length,before);
 await assert.rejects(()=>R.mutate('timer-a','start',{duration_seconds:1}),/연결/);
 // Individual sound opt-in is independent, no AudioContext hardware required.
 context.AudioContext=class{constructor(){this.state='running';}async resume(){}};
 await R.enableSound('timer-a');assert.equal(R.soundStatus('timer-a'),'소리 허용됨');assert.equal(R.soundStatus('timer-b'),'소리 꺼짐');
 await R.enableSound('timer-b');R.mute('timer-a');assert.equal(R.soundStatus('timer-a'),'소리 꺼짐');assert.equal(R.soundStatus('timer-b'),'소리 허용됨');
 R.mute();assert.equal(R.soundStatus(),'소리 꺼짐');
 console.log('PASS timer transport: duplicate time, reconnect, two widgets, in-flight poll, idempotency, rejection, clock drift, revocation, per-widget sound');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
