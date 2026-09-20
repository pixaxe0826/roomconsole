/* Room Hub 0.1.3: explicit foreground recording, same-origin upload, no cloud STT.
   Kept outside widget re-rendering so live updates cannot destroy a recording. */
(() => {
'use strict';
const $=s=>document.querySelector(s),E=Room.esc,demo=!!window.ROOM_DEMO;
let modal=null,stream=null,recorder=null,chunks=[],recordTimer=null,pollTimer=null,blob=null,blobURL=null;
let phase='idle',job=null,cfg=null,requestID='',opened=false,epoch=0,startedAt=0,returnFocus=null,discard=false,busy=false;
const labels={queued:'V35 전사 대기 중',running:'V35에서 전사하고 있어요',succeeded:'전사가 완료되었어요',failed:'전사하지 못했어요',cancelled:'전사를 취소했어요'};
function clearTimer(){clearInterval(recordTimer);recordTimer=null;clearTimeout(pollTimer);pollTimer=null}
function tracksOff(){if(stream)stream.getTracks().forEach(t=>t.stop());stream=null}
function releaseBlob(){if(blobURL)URL.revokeObjectURL(blobURL);blobURL=null;blob=null;chunks=[]}
function persist(id){try{if(id)sessionStorage.setItem('room-speech-job',id);else sessionStorage.removeItem('room-speech-job')}catch{}}
function remembered(){try{return sessionStorage.getItem('room-speech-job')}catch{return null}}
function uid(){return crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+'-'+Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('')}
async function api(path,options={}){
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),options.timeout||12000);
 try{
  const r=await fetch(path,{method:options.method||'GET',credentials:'same-origin',headers:{'X-Room-Request':'1'},body:options.body,signal:controller.signal});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){const err=Error(typeof data.detail==='string'?data.detail:'요청을 처리하지 못했습니다.');err.status=r.status;throw err}
  return data;
 }catch(e){if(e.name==='AbortError')throw Error('연결 응답이 늦습니다. Wi-Fi를 확인한 뒤 같은 녹음을 다시 보내세요.');throw e}
 finally{clearTimeout(timeout)}
}
function message(text){const el=$('#speechMessage');if(el)el.textContent=text}
function shell(){
 if(modal)return;
 modal=document.createElement('section');modal.className='speech-overlay hidden';modal.id='speechPanel';modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');modal.setAttribute('aria-labelledby','speechTitle');
 modal.innerHTML=`<div class="speech-card"><header class="speech-head"><div><small>VOICE TO YOUR ROOM</small><h2 id="speechTitle">말로 남기는 메모</h2></div><button class="btn" id="speechClose" aria-label="음성 화면 닫기">← 뒤로</button></header><div class="speech-body" id="speechBody"></div><p id="speechMessage" class="speech-message" role="status" aria-live="polite"></p><footer class="speech-privacy">버튼을 누를 때만 녹음 · V35 내부 전사 · 명령 자동 실행 없음</footer></div>`;
 document.body.append(modal);$('#speechClose').onclick=close;
 modal.addEventListener('keydown',e=>{
  if(e.key==='Escape'){e.preventDefault();close()}
  if(e.key==='Tab'){
   const nodes=[...modal.querySelectorAll('button:not([disabled]),audio[controls],a[href]')].filter(x=>x.getClientRects().length);
   const first=nodes[0],last=nodes[nodes.length-1];
   if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}
   else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}
  }
 });
}
function body(html){$('#speechBody').innerHTML=html;message('')}
function supported(){
 if(self!==top)return '관리자 안의 축소 미리보기에서는 녹음하지 않습니다. iPad의 클라이언트 페이지를 직접 여세요.';
 if(!isSecureContext)return '마이크에는 신뢰된 HTTPS가 필요합니다. 인증서를 설치·신뢰한 뒤 https://192.168.0.14:8443/client 로 접속하세요.';
 if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)return '이 브라우저에서는 녹음 API를 사용할 수 없습니다. iPadOS/Safari를 업데이트하고 직접 페이지를 여세요.';
 return '';
}
function showReady(note=''){
 phase='idle';body(`<div class="speech-hero">${Room.icon('mic',44)}<h3>무엇을 남겨 볼까요?</h3><p>“내일 택배 보내기”처럼 짧게 말해 보세요.<br>녹음을 마친 뒤 직접 전송합니다.</p></div><div class="speech-chips"><span>${E(cfg?.model||'Whisper · 로컬')}</span><span>최대 ${cfg?.max_seconds||30}초</span><span>한국어</span></div><button class="btn primary speech-primary" id="speechStart">${Room.icon('mic',18)} 녹음 시작</button><button class="btn speech-secondary" id="speechPrevious">최근 전사 확인</button>`);
 $('#speechStart').onclick=record;$('#speechPrevious').onclick=recent;
 if(note)message(note);
}
function showBlocked(text){phase='blocked';body(`<div class="speech-hero">${Room.icon('mic',44)}<h3>녹음 준비를 확인해 주세요</h3><p>${E(text)}</p></div><button class="btn speech-primary" id="speechCheck">준비 상태 다시 확인</button>`);$('#speechCheck').onclick=load}
async function load(){
 const current=++epoch;phase='checking';body('<div class="speech-hero"><h3>V35의 전사 준비 상태를 확인하고 있어요</h3></div>');
 if(demo){cfg={model:'tiny · 예시',max_seconds:30};showBlocked('독립 HTML은 화면 확인용입니다. 실제 녹음은 V35 HTTPS 클라이언트에서 사용하세요.');return}
 try{
  cfg=await api('/api/speech/status');if(!opened||current!==epoch)return;
  const previous=remembered();
  if(previous){try{job=await api('/api/speech/jobs/'+encodeURIComponent(previous));if(!opened||current!==epoch)return;showJob();return}catch(e){if(e.status===404)persist(null)}}
  const reason=supported()||(!cfg.ready&&cfg.reason);if(reason)showBlocked(reason);else showReady();
 }catch(e){if(opened&&current===epoch)showBlocked(e.message)}
}
async function open(){
 if(opened)return;shell();opened=true;returnFocus=document.activeElement;modal.classList.remove('hidden');document.body.classList.add('speech-open');$('#speechClose').focus();await load();
}
function close(){
 if(busy&&phase==='uploading'){message('전송 결과를 확인하고 있습니다. 잠시 기다려 주세요.');return}
 opened=false;epoch++;discard=true;busy=false;clearTimer();
 if(recorder&&recorder.state!=='inactive'){try{recorder.stop()}catch{}}
 tracksOff();releaseBlob();modal?.classList.add('hidden');document.body.classList.remove('speech-open');returnFocus?.focus();
 // Closing a server job view does not silently cancel that job. It is resumable by id.
}
function chooseMime(){
 for(const mime of ['audio/mp4','audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus']){
  try{if(MediaRecorder.isTypeSupported?.(mime))return mime}catch{}
 }
 return '';
}
async function record(){
 const reason=supported();if(reason){showBlocked(reason);return}
 if(busy||phase==='recording')return;
 busy=true;phase='permission';const current=++epoch;releaseBlob();discard=false;job=null;persist(null);
 body('<div class="speech-hero"><h3>마이크 접근을 허용해 주세요</h3><p>Safari의 권한 창에서 마이크 사용을 허용합니다.</p></div>');
 try{
  const input=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1},video:false});
  if(!opened||current!==epoch){input.getTracks().forEach(t=>t.stop());return}
  stream=input;chunks=[];requestID=uid();const mime=chooseMime();
  recorder=new MediaRecorder(stream,mime?{mimeType:mime}:undefined);
  const localRecorder=recorder;let localChunks=[];
  recorder.ondataavailable=e=>{if(current!==epoch)return;if(e.data&&e.data.size)localChunks.push(e.data);if(localChunks.reduce((n,b)=>n+b.size,0)>(cfg.max_bytes||8*1024*1024)){discard=true;stopRecord();showReady('녹음 용량 제한을 초과했습니다. 더 짧게 녹음하세요.')}};
  recorder.onerror=()=>{discard=true;stopRecord();if(opened){showReady();message('녹음이 중단되었습니다. 마이크와 Safari 상태를 확인하세요.')}};
  recorder.onstop=()=>{
   input.getTracks().forEach(t=>t.stop());
   if(current!==epoch)return;
   clearInterval(recordTimer);recordTimer=null;tracksOff();busy=false;
   if(!opened||discard){releaseBlob();return}
   blob=new Blob(localChunks,{type:localRecorder.mimeType||mime||'audio/mp4'});localChunks=[];
   if(!blob.size){showReady('빈 녹음입니다. 다시 녹음하세요.');return}
   blobURL=URL.createObjectURL(blob);phase='recorded';showRecorded();
  };
  input.getTracks().forEach(t=>t.onended=()=>{if(phase==='recording'){discard=true;stopRecord();showReady('마이크가 중단되었습니다. 녹음은 전송하지 않았습니다.')}});
  recorder.start(1000);startedAt=Date.now();phase='recording';busy=false;
  body(`<div class="speech-hero recording"><span class="speech-rec-dot"></span><h3>듣고 있어요</h3><div id="speechElapsed" class="speech-time">00:00</div><p>최대 ${cfg.max_seconds}초 · 종료 후 전송 전 다시 들을 수 있어요.</p></div><button class="btn primary speech-primary" id="speechStop">녹음 종료</button><button class="btn speech-secondary" id="speechDiscard">취소 · 전송하지 않기</button>`);
  $('#speechStop').onclick=stopRecord;$('#speechDiscard').onclick=()=>{discard=true;stopRecord();showReady('녹음을 취소했습니다. 서버에 전송하지 않았습니다.')};
  recordTimer=setInterval(()=>{const secs=(Date.now()-startedAt)/1000;if($('#speechElapsed'))$('#speechElapsed').textContent='00:'+Math.floor(secs).toString().padStart(2,'0');if(secs>=cfg.max_seconds-.25)stopRecord()},150);
 }catch(e){tracksOff();if(opened&&current===epoch){showReady();message(e.name==='NotAllowedError'?'마이크 권한이 거부되었습니다. Safari 웹사이트 설정의 마이크 권한과 HTTPS 인증서 신뢰를 확인하세요.':e.name==='NotFoundError'?'사용 가능한 마이크를 찾지 못했습니다.':'녹음을 시작하지 못했습니다. 다른 앱의 마이크 사용 여부를 확인하세요.')}}
 finally{busy=false}
}
function stopRecord(){
 if(phase==='recording'){phase='stopping';busy=true}clearInterval(recordTimer);recordTimer=null;
 if(recorder&&recorder.state!=='inactive'){try{recorder.stop()}catch{}}
 // Release the microphone immediately, do not wait for the HTTP request.
 tracksOff();
}
function showRecorded(note=''){
 body(`<div class="speech-hero">${Room.icon('check',40)}<h3>녹음을 확인해 주세요</h3><p>전사하기를 누르면 이 녹음만 V35에 전송합니다.</p></div><audio class="speech-audio" controls playsinline src="${blobURL}"></audio><button class="btn primary speech-primary" id="speechSend">V35에서 전사하기</button><button class="btn speech-secondary" id="speechAgain">녹음 다시 하기</button>`);
 $('#speechSend').onclick=upload;$('#speechAgain').onclick=()=>{releaseBlob();showReady()};if(note)message(note);
}
async function upload(){
 if(busy||!blob)return;busy=true;phase='uploading';$('#speechSend').disabled=true;message('V35에 녹음을 전송하고 있습니다…');
 const form=new FormData();form.append('request_id',requestID);form.append('file',blob,blob.type.includes('mp4')?'recording.m4a':'recording.webm');
 try{job=await api('/api/speech/jobs',{method:'POST',body:form,timeout:45000});persist(job.id);releaseBlob();showJob()}
 catch(e){phase='recorded';showRecorded(e.status===401?'기기 연결 권한이 만료되었습니다. 관리자에서 다시 연결하세요.':e.message+' 녹음은 이 탭에 남아 있습니다. 재전송은 같은 요청 ID를 사용합니다.')}
 finally{busy=false}
}
function showJob(){
 if(!opened||!job)return;phase='job';clearTimeout(pollTimer);
 const active=['queued','running'].includes(job.status);
 body(`<div class="speech-hero"><div class="speech-orb ${active?'busy':''}">${Room.icon(active?'mic':'check',36)}</div><h3>${E(labels[job.status]||job.status)}</h3><p>${active?'화면을 닫아도 V35에서 계속 처리합니다. 다른 위젯은 사용할 수 있어요.':'전사 결과는 관리자의 음성 수신함에도 보관됩니다.'}</p></div>${job.text?`<div class="speech-transcript" id="speechTranscript">${E(job.text)}</div>`:''}${job.error?`<p class="speech-error">${E(job.error)}</p>`:''}${job.elapsed!=null?`<p class="speech-meta">서버 처리 ${Number(job.elapsed).toFixed(1)}초${job.duration!=null?' · 녹음 '+Number(job.duration).toFixed(1)+'초':''}</p>`:''}<p class="speech-disclaimer">전사는 틀릴 수 있습니다. 할 일 추가·메일 발송 등은 자동 실행하지 않습니다.</p>${active?'<button class="btn speech-primary" id="speechCancelJob">전사 취소</button>':`${['failed','cancelled'].includes(job.status)?'<button class="btn speech-primary" id="speechRetryJob">같은 녹음 전사 재시도</button>':''}<button class="btn primary speech-primary" id="speechNew">새로 녹음하기</button>`}`);
 $('#speechCancelJob')?.addEventListener('click',()=>action('cancel'));$('#speechRetryJob')?.addEventListener('click',()=>action('retry'));
 $('#speechNew')?.addEventListener('click',()=>{job=null;persist(null);load()});
 if(active)pollTimer=setTimeout(poll,1500);
}
async function poll(){
 if(!opened||!job)return;const id=job.id,current=epoch;
 try{const next=await api('/api/speech/jobs/'+encodeURIComponent(id));if(opened&&epoch===current&&job?.id===id){job=next;showJob()}}
 catch(e){if(!opened||epoch!==current)return;message(e.status===401?'연결 권한이 만료되었습니다. 새 기기 연결이 필요합니다.':'연결을 기다리고 있어요. Wi-Fi가 돌아오면 전사 상태를 다시 확인합니다.');if(e.status!==401&&e.status!==404)pollTimer=setTimeout(poll,4000)}
}
async function action(name){if(busy||!job)return;busy=true;try{job=await api('/api/speech/jobs/'+job.id+'/'+name,{method:'POST'});showJob()}catch(e){message(e.message)}finally{busy=false}}
async function recent(){try{const data=await api('/api/speech/jobs');if(data.jobs.length){job=data.jobs[0];persist(job.id);showJob()}else message('아직 이 기기에서 보낸 녹음이 없습니다.')}catch(e){message(e.message)}}
window.addEventListener('pagehide',()=>{discard=true;stopRecord();tracksOff()});
window.addEventListener('online',()=>{if(opened&&job)poll()});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&['permission','recording','stopping'].includes(phase)){epoch++;discard=true;stopRecord();tracksOff();busy=false;if(opened)showReady('화면이 숨겨져 녹음을 중단했습니다. 전송하지 않았습니다.')}else if(!document.hidden&&opened&&job)poll()});
document.querySelectorAll('[data-open-speech]').forEach(b=>b.addEventListener('click',open));
window.RoomSpeech={open,close};
})();
