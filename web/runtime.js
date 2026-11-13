/* Manager-only status UI. All dynamic values use textContent, never HTML. */
(() => {
'use strict';
const $=id=>document.getElementById(id);
const labels={online:'최근 연결 확인 성공',offline:'연결 끊김',unknown:'미확인 / 확인 만료',configured:'로컬 설치 경로 확인',unavailable:'사용 불가',loading:'로딩 중',timeout:'확인 시간 초과',model_mismatch:'모델 ID 불일치',config_error:'설정 오류',probe_error:'연결 확인 오류',http_error:'HTTP 오류',invalid_response:'응답 형식 오류'};
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
  $('message').textContent=probe?'연결 확인이 끝났습니다. 실제 추론은 음성/LLM 요청으로 별도 확인하세요.':'';
 }catch(e){$('message').textContent=e.name==='AbortError'?'진단 요청 시간이 초과됐습니다. 자동 재전송하지 않습니다.':e.message}
 finally{busy=false;$('probe').disabled=false}
}
$('probe').addEventListener('click',()=>refresh(true));
refresh();setInterval(()=>{if(!document.hidden)refresh()},5000);
})();
