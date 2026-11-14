"""Production speech UI + mocked same-origin API; no microphone/cloud calls.

Runs Chromium on synthetic set_content fixtures. This verifies DOM rendering and
GET-only lifecycle handling, not physical iPad/Safari, recognition, or deployment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / 'artifacts' / 'test-results'
SHOTS = ROOT / 'artifacts' / 'screenshots'


FIXTURE = r"""
() => {
 const memory = new Map([['room-speech-job', 'synthetic-job']]);
 Object.defineProperty(window, 'sessionStorage', {value: {
   getItem: key => memory.get(key) ?? null,
   setItem: (key,value) => memory.set(key,String(value)),
   removeItem: key => memory.delete(key)
 }});
 Object.defineProperty(window, 'isSecureContext', {value:true});
 Object.defineProperty(navigator, 'mediaDevices', {value:{getUserMedia:async()=>{throw Error('No microphone allowed in read-only tests');}}});
 window.MediaRecorder = class {};
 window.__speechCalls = [];
 window.__speechPolls = new Map();
 window.__speechNext = 100000;
 window.__speechCfg = {ready:true, backend:'openai', agent_enabled:true,
                       model:'synthetic-transcribe', max_seconds:30, max_bytes:1024};
 window.__speechJob = {id:'synthetic-job', status:'queued', text:'', agent_run:null};
 window.__speechFailure = null;
 const originalSetTimeout=window.setTimeout,originalClearTimeout=window.clearTimeout;
 window.setTimeout = (fn, ms, ...args) => {
   if(ms===1500 || ms===4000){const key=++window.__speechNext;window.__speechPolls.set(key,{fn,ms});return key;}
   return originalSetTimeout(fn,ms,...args);
 };
 window.clearTimeout = id => {window.__speechPolls.delete(id);originalClearTimeout(id);};
 window.__speechTick = async() => {
   const first=window.__speechPolls.entries().next().value;
   if(!first)throw Error('Expected a scheduled read-only poll');
   window.__speechPolls.delete(first[0]);await first[1].fn();
 };
 window.fetch=async(path,options={})=>{
   window.__speechCalls.push({path,method:options.method||'GET'});
   if((options.method||'GET')!=='GET')throw Error('Unexpected mutation during polling/reconnect');
   if(path==='/api/speech/status')return {ok:true,status:200,json:async()=>structuredClone(window.__speechCfg)};
   if(path==='/api/speech/jobs/synthetic-job'){
     if(window.__speechFailure!==null){const error=window.__speechFailure;window.__speechFailure=null;if(error===0)throw Error('synthetic network loss');return {ok:false,status:error,json:async()=>({detail:'Synthetic request denied'})};}
     return {ok:true,status:200,json:async()=>structuredClone(window.__speechJob)};
   }
   throw Error('Unexpected fixture URL '+path);
 };
}
"""


def run():
    REPORTS.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({'name': name, 'passed': True})

    with sync_playwright() as playwright:
        executable = os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome')
        browser = playwright.chromium.launch(executable_path=executable, headless=True,
                                            args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 834, 'height': 1194})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<!doctype html><html><body><button data-open-speech>음성 요청</button></body></html>')
        page.add_style_tag(content=(ROOT / 'web' / 'base.css').read_text(encoding='utf-8'))
        page.add_style_tag(content=(ROOT / 'web' / 'speech.css').read_text(encoding='utf-8'))
        page.evaluate(FIXTURE)
        page.add_script_tag(content=(ROOT / 'web' / 'shared.js').read_text(encoding='utf-8'))
        page.add_script_tag(content=(ROOT / 'web' / 'speech.js').read_text(encoding='utf-8'))
        page.evaluate('RoomSpeech.open()')
        check('Queued transcription schedules status poll', page.evaluate('__speechPolls.size') == 1)
        check('Backend wording reflects explicit OpenAI configuration', 'OpenAI 클라우드' in page.locator('#speechPrivacy').inner_text())

        page.evaluate("__speechJob={...__speechJob,status:'succeeded',text:'오늘 할 일 확인',agent_run:{status:'pending'}};__speechTick()")
        check('STT success does not stop while agent is pending', page.evaluate('__speechPolls.size') == 1)
        check('Pending agent is visibly distinct from completed transcription', '개인 비서 요청을 준비' in page.locator('#speechBody').inner_text())
        page.evaluate("__speechJob.agent_run={id:'synthetic-run',status:'running',summary_text:''};__speechTick()")
        check('Agent running continues status polling', page.evaluate('__speechPolls.size') == 1)
        page.evaluate("__speechJob.agent_run={id:'synthetic-run',status:'awaiting_confirmation',summary_text:'Synthetic proposed change',awaiting_admin_confirmation:true};__speechTick()")
        check('Manager confirmation requirement displayed', '관리자 확인을 기다리고' in page.locator('#speechBody').inner_text())
        check('Confirmation wait continues polling without display approval controls', page.evaluate('__speechPolls.size') == 1 and page.locator('#speechBody button').count() == 1 and page.locator('#speechNew').count() == 1)
        page.screenshot(path=str(SHOTS / 'agent-speech-awaiting-confirmation.png'))

        page.evaluate('RoomSpeech.close()')
        check('Closing clears polls but preserves job identity', page.evaluate("__speechPolls.size===0 && sessionStorage.getItem('room-speech-job')==='synthetic-job'"))
        page.evaluate('RoomSpeech.open()')
        check('Reopening resumes the same job using GET', page.evaluate('__speechPolls.size') == 1)
        page.evaluate("__speechFailure=503;__speechTick()")
        check('Transient network/API failure schedules a read-only reconnect', page.evaluate('__speechPolls.size') == 1)
        page.evaluate("__speechJob.agent_run={id:'synthetic-run',status:'succeeded',summary_text:'<img src=x onerror=window.PWNED=true>\\n완료',redacted:false};__speechTick()")
        check('Completed agent stops polling', page.evaluate('__speechPolls.size') == 0)
        check('Agent summary uses escaped text not injected HTML', page.locator('#speechAgentResult img').count() == 0 and '<img' in page.locator('#speechAgentResult').inner_text() and not page.evaluate('!!window.PWNED'))
        check('Lifecycle only uses GET; no duplicate submit or confirmation', page.evaluate("__speechCalls.every(call=>call.method==='GET')"))

        page.evaluate("__speechJob.agent_run={id:'synthetic-run',status:'interrupted',summary_text:'내용은 관리자 기록에서 확인하세요.',redacted:true};window.dispatchEvent(new Event('online'))")
        page.wait_for_function("document.querySelector('#speechBody').textContent.includes('자동 재실행하지 않습니다')")
        check('Interrupted/unknown result is not retried', page.evaluate('__speechPolls.size') == 0 and page.locator('#speechRetryJob').count() == 0)
        check('Private result carries manager-only notice', '공유되지 않은 내용' in page.locator('#speechAgentResult').inner_text())
        page.evaluate("RoomSpeech.close();__speechCfg.backend='remote_http';__speechJob.agent_run=null;RoomSpeech.open()")
        check('Legacy STT-only response remains terminal', page.evaluate('__speechPolls.size') == 0 and page.locator('#speechAgentResult').count() == 0)
        check('Remote Whisper wording does not claim V35-local processing', '원격 Whisper' in page.locator('#speechPrivacy').inner_text() and 'V35' not in page.locator('#speechPanel').inner_text())

        page.evaluate("RoomSpeech.close();__speechJob.agent_run={status:'pending'};RoomSpeech.open()")
        page.evaluate("__speechFailure=401;__speechTick()")
        check('Revoked session stops polling without automatic re-pair or resubmit', page.evaluate('__speechPolls.size') == 0 and '만료' in page.locator('#speechMessage').inner_text())
        check('No uncaught browser errors', not errors)
        browser.close()

    report = {'passed': len(checks), 'checks': checks,
              'scope': 'Production speech/shared JS in Chromium set_content; mocked same-origin HTTP and timers; no paid calls, microphone, physical device, or live backend.'}
    (REPORTS / 'agent-speech-browser.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'passed': len(checks), 'scope': report['scope']}, ensure_ascii=False))


if __name__ == '__main__':
    run()
