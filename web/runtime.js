/* Manager-only status UI. All dynamic values use textContent, never HTML. */
(() => {
'use strict';
const $=id=>document.getElementById(id);
const labels={online:'최근 연결 확인 성공',offline:'연결 끊김',unknown:'미확인 / 확인 만료',configured:'로컬 설치 경로 확인',unavailable:'사용 불가',loading:'로딩 중',timeout:'확인 시간 초과',model_mismatch:'모델 ID 불일치',config_error:'설정 오류',probe_error:'연결 확인 오류',http_error:'HTTP 오류',invalid_response:'응답 형식 오류',agent_disabled:'Personal Agent 꺼짐',cloud_disabled:'클라우드 사용 꺼짐',audio_cloud_disabled:'음성 외부 전송 꺼짐',api_key_missing:'OpenAI API 키 미설정',missing_api_key:'OpenAI API 키 미설정',budget_exhausted:'API 예산 한도 도달',budget_hard_cap_reached:'API 예산 한도 도달',profile_endpoint_blocked:'OpenAI 공식 API 주소 확인 필요',live_network_blocked_in_test:'테스트 환경의 외부 호출 차단',speech_unavailable:'전사 전처리/worker 준비 필요'};
let busy=false;
async function api(path,method='GET') {
 const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),12000);
 try {
  const r=await fetch(path,{method,credentials:'same-origin',headers:{'X-Room-Request':'1'},signal:ctrl.signal});
  if(!r.ok)throw Error(r.status===401?'관리자 로그인이 필요합니다. 관리자 화면에서 로그인하세요.':'서버 요청을 처리하지 못했습니다.');
  return await r.json();
 } finally {clearTimeout(timer)}
}
function rows(id,items) {
 const box=$(id);box.replaceChildren();
 for(const [label,value] of items){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value==null?'미확인':String(value);box.append(dt,dd)}
}
function paint(data){
 const s=data.speech||{},l=data.llm||{};
 const pcFree=data.runtime_profile==='odyssey_openai';
 $('architecture').textContent=pcFree?'iPad → Odyssey → OpenAI API → Odyssey 처리. PC·Whisper/LLM 서버·SSH 터널은 사용하지 않으며 장애 시에도 되돌아가지 않습니다.':'Room Hub 기본 서비스와 기존 로컬/원격 AI 연결을 구분합니다. 모델 실행·재시작·다운로드는 하지 않습니다.';
 $('probe').textContent=pcFree?'설정 상태 새로고침':'전사·LLM 연결 확인';
 $('scopeNote').textContent=pcFree?'이 화면은 Odyssey의 정책·키 존재 여부·예산·FFmpeg/worker 조건만 확인합니다. 새로고침도 PC나 OpenAI에 접속하지 않습니다. 실제 API 접근 권한·음성 전사·응답·청구는 명시적으로 요청한 뒤 확인해야 합니다.':'연결 확인은 전사 서버의 /health와 LLM 서버의 /v1/models만 조회합니다. 성공해도 실제 음성 인식·토큰 생성·CUDA 사용을 증명하지 않습니다. 30초가 지난 결과는 미확인으로 표시하며 자동으로 모델을 호출하지 않습니다.';
 if(pcFree){
  const state=v=>v==='configured'?'설정 조건 충족 · 실제 API 호출 미검증':labels[v]||v;
  rows('speech',[
   ['음성 외부 전송',s.enabled?'허용':'꺼짐'],['준비 상태',state(s.state)],['Backend',s.backend],
   ['설정 모델',s.configured_model],['전처리',s.preprocessing],['PC/SSH 의존성','없음'],
   ['로컬/PC 대체 전사','차단'],['검증 범위',s.identity_note],['메시지',s.message]
  ]);
  rows('llm',[
   ['클라우드 계획',l.enabled?'허용':'꺼짐'],['준비 상태',state(l.state)],['Backend',l.backend],
   ['빠른 계획 모델',l.configured_model],['심층 계획 모델',l.configured_deep_model],
   ['추론 강도',l.reasoning_effort],['실행·저장·승인','Odyssey'],['PC/SSH 의존성','없음'],
   ['검증 범위',l.identity_note]
  ]);
  return;
 }
 rows('speech',[
  ['연결 상태',labels[s.state]||s.state],['Backend',s.backend],['설정 모델',s.configured_model],
  ['API 보고 모델',s.reported_model],['설정 장치',s.configured_device],['API 보고 장치',s.reported_device],
  ['확인 시각',s.last_probe?.checked_at],['검증 범위',s.identity_note],['메시지',s.last_probe?.message]
 ]);
 rows('llm',[
  ['전송 사용',l.enabled?'켜짐':'꺼짐'],['연결 상태',labels[l.state]||l.state],['설정 모델',l.configured_model],
  ['API 모델 목록',(l.reported_models||[]).join('\n')||null],['모델 ID 일치',l.model_list_match?'최근 목록에서 일치':'미확인 / 불일치'],
  ['API 보고 장치',l.reported_device],['확인 시각',l.last_probe?.checked_at],['검증 범위',l.identity_note]
 ]);
}
async function refresh(probe=false){
 if(busy)return;busy=true;$('probe').disabled=true;
 try{
  const [data,health]=await Promise.all([api(probe?'/api/ai/probe':'/api/ai/status',probe?'POST':'GET'),api('/healthz')]);
  paint(data);$('core').textContent=health.status==='ok'?'Room Hub 기본 서버 응답 정상':'기본 서버 상태 확인 필요';
  $('message').textContent=probe?(data.runtime_profile==='odyssey_openai'?'로컬 설정을 다시 확인했습니다. PC/OpenAI 연결이나 유료 추론은 실행하지 않았습니다.':'연결 확인이 끝났습니다. 실제 추론은 음성/LLM 요청으로 별도 확인하세요.'):'';
 }catch(e){$('message').textContent=e.name==='AbortError'?'진단 요청 시간이 초과됐습니다. 자동 재전송하지 않습니다.':e.message}
 finally{busy=false;$('probe').disabled=false}
}
$('probe').addEventListener('click',()=>refresh(true));
refresh();setInterval(()=>{if(!document.hidden)refresh()},5000);
})();
