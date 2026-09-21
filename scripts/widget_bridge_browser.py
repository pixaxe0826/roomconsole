"""Real Uvicorn/SQLite/Adapters + counted fake LLM + existing browser HTTP bridge.

Browser networking and WS invalidations are explicitly adapted; no physical V35,
iPad, microphone, real model or inference latency is tested here.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import httpx
import uvicorn
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('HUB_DATA_DIR',str(ROOT/'artifacts/runtime-data'))
from assistant_browser import load_bridged
from app.main import create_app
from app.llm import sha


class FakeModel:
    def __init__(self):self.calls=[];self.output='invalid'
    async def generate(self, endpoint, body, timeout):
        self.calls.append(json.loads(body))
        value={'choices':[{'message':{'content':self.output},'finish_reason':'stop'}], 'model':'SYNTHETIC-NOT-QWEN'}
        return value,json.dumps(value,ensure_ascii=False)


def main():
    result={'scope':__doc__,'checks':[],'page_errors':[]}
    def check(name, yes):
        assert yes,name
        result['checks'].append(name);print('PASS',name,flush=True)
    with tempfile.TemporaryDirectory() as td:
        model=FakeModel();app=create_app(Path(td)/'data',weather_enabled=False,llm_backend=model)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,
                headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
                state=api.get('/api/state').json();layout=state['layout'];layout['rows']=8
                layout['widgets'] += [
                    {'id':'memo-test','type':'note','title':'합성 메모','x':0,'y':6,'w':4,'h':2,'config':{}},
                    {'id':'reply-test','type':'llm-response','title':'합성 결과','x':4,'y':6,'w':4,'h':2,'config':{}},
                ]
                assert api.put('/api/layout',json=layout).status_code==200
                assert api.post('/api/life/notes',json={'request_id':'browser-memo-01','title':'합성',
                    'body':'실제 저장 본문 <script>window.BRIDGE_PWNED=true</script>','shared':True}).status_code==201
                cfg=api.get('/api/llm/config').json()['config'];cfg['enabled']=True
                assert api.put('/api/llm/config',json=cfg).status_code==200
                def submit(text, output=None):
                    if output is not None:model.output=json.dumps(output,ensure_ascii=False) if isinstance(output,dict) else output
                    key='widget-ui-'+str(time.time_ns())
                    v=api.post('/api/voice/text',json={'request_id':key,'source':'synthetic','text':text}).json()
                    r=api.post('/api/llm/requests',json={'request_id':key,'voice_id':v['id'],'expected_text_sha256':sha(text),'mode':'auto'})
                    assert r.status_code==202,r.text
                    d=r.json()
                    for _ in range(100):
                        d=api.get('/api/llm/requests/'+d['id']).json()
                        if d['status'] not in {'queued','running'}:return d
                        time.sleep(.02)
                    raise AssertionError('Request did not settle')
                fast=submit('오늘 할 일 알려줘')
                check('Fast-path uses TodoAdapter without model',not model.calls and fast['assistant']['widget_trace']['adapter']=='TodoAdapter')
                memo=submit('메모 내용 좀 읽어 볼래',{'widget':'memo','action':'read','target':None,'args':{}})
                task=submit('내일 하늘에 우유 사기 추가해',{'widget':'todo','action':'add','target':None,
                    'args':{'date':api.get('/api/clock').json()['tomorrow'],'title':'우유 사기','time':None}})
                check('Fallback write stops before mutation',task['status']=='awaiting_confirmation' and not app.state.store.tasks())
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1100})
                        page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        load_bridged(page,api,'manager');page.wait_for_selector('.kpis')
                        page.evaluate("RoomManager.navigate('llm')")
                        def select(d):
                            page.locator(f'[data-llm=select][data-id="{d["id"]}"]').click()
                            page.locator('.llm-detail-heading').filter(has_text=d['id'][:8]).wait_for()
                        select(memo)
                        panel=page.locator('[data-widget-protocol]')
                        panel.filter(has_text='MemoAdapter').wait_for()
                        check('Domain and scoped capability visible','memo.read' in panel.inner_text())
                        check('Actual source text is the final answer',page.locator('.llm-output').inner_text()==memo['response_json']['output'])
                        check('Memo HTML is escaped',page.evaluate('window.BRIDGE_PWNED!==true'))
                        select(task)
                        page.wait_for_selector('[data-widget-preview]')
                        check('Protocol preview contains exact title/date','우유 사기' in page.locator('[data-widget-preview]').inner_text() and 'time' in page.locator('[data-widget-preview]').inner_text())
                        check('Policy is needs_confirmation','needs_confirmation' in panel.inner_text())
                        check('Nothing changed before click',not app.state.store.tasks())
                        page.locator('[data-llm=confirm-action]').click()
                        page.wait_for_selector('[data-agent-state=succeeded]')
                        check('Confirm executed one actual Adapter task',len(app.state.store.tasks())==1 and app.state.store.tasks()[0]['title']=='우유 사기')
                        check('Success text is deterministic','할 일 1개를 등록했습니다.' in page.locator('.llm-output').inner_text())
                        page.locator('details[data-section=agent]').evaluate('(e)=>e.open=true')
                        check('Request/Response/schema diagnostic preserved','widget_response' in page.locator('details[data-section=agent]').inner_text())
                        check('Exactly two model calls, not a formatter call',len(model.calls)==2)
                        check('Actual request is schema constrained',all(x['response_format']['type']=='json_schema' and x['max_tokens']==64 for x in model.calls))
                        candidate=submit('현재 메론 내용 읽어줘.',{'widget':'memo','action':'read','target':None,'args':{}})
                        page.evaluate('RoomLLM.refresh()');select(candidate)
                        check('Unknown read noun uses CANDIDATE parser','CANDIDATE' in page.locator('[data-routing]').inner_text() and len(model.calls)==3)
                        check('Candidate reads actual memo, not model prose',page.locator('.llm-output').inner_text()==candidate['response_json']['output'])
                        alarm=submit('내일 아침 7시 25분 알람 맞춰.')
                        page.evaluate('RoomLLM.refresh()');select(alarm)
                        check('Exact alarm uses local rule and no extra model','EXACT' in page.locator('[data-routing]').inner_text() and len(model.calls)==3)
                        check('Alarm preview is 07:25 with no reservation before confirmation','07:25' in page.locator('[data-widget-preview]').inner_text() and not app.state.life.alarms())
                        page.locator('[data-llm=confirm-action]').click();page.wait_for_selector('[data-agent-state=succeeded]')
                        check('Confirmed alarm is in actual service',len(app.state.life.alarms())==1 and app.state.life.alarms()[0]['time']=='07:25')
                        check('Alarm response does not claim actual sound','실제 소리 재생을 확인한 것은 아닙니다' in page.locator('.llm-output').inner_text())
                        check('No uncaught browser errors',not result['page_errors'])
                        shots=ROOT/'artifacts/screenshots';shots.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(shots/'widget-bridge-diagnostics.png'),full_page=True)
                    finally:browser.close()
        finally:server.should_exit=True;thread.join(timeout=10)
    result['passed']=len(result['checks']);out=ROOT/'artifacts/test-results';out.mkdir(parents=True,exist_ok=True)
    (out/'widget-bridge-browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('TOTAL',result['passed'])


if __name__=='__main__':main()
