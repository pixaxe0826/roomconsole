"""Synthetic named memo tests: real loopback HTTP + browser fetch bridge.

No V35, Safari, microphone, model or production data. HTML bodies remain data;
private snapshots are never projected to the paired-display response endpoint.
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('HUB_DATA_DIR',str(ROOT/'artifacts/runtime-data'))
from assistant_browser import load_bridged
from app.main import create_app
from app.llm import sha


class NoModel:
    def __init__(self):self.calls=[]
    async def generate(self,*args,**kwargs):
        self.calls.append(True)
        raise AssertionError('Named-memo exact route must not call a model')


def main():
    results={'scope':__doc__,'checks':[],'page_errors':[]}
    def check(name,value):
        assert value,name
        results['checks'].append(name);print('PASS',name,flush=True)
    with tempfile.TemporaryDirectory() as td:
        model=NoModel();app=create_app(Path(td)/'test',weather_enabled=False,llm_backend=model)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,
                    headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
                layout=api.get('/api/state').json()['layout'];layout['rows']=10
                layout['widgets'] += [
                    {'id':'catalog-note','type':'note','title':'합성 메모','x':0,'y':6,'w':4,'h':2,'config':{}},
                    {'id':'catalog-reply','type':'llm-response','title':'합성 응답','x':4,'y':6,'w':4,'h':2,'config':{}},
                ]
                assert api.put('/api/layout',json=layout).status_code==200
                def note(title,body,shared):
                    r=api.post('/api/life/notes',json={'request_id':'note-'+str(time.time_ns()),'title':title,'body':body,'shared':shared})
                    assert r.status_code==201,r.text
                    return r.json()['id']
                def submit(text):
                    key='catalog-'+str(time.time_ns())
                    voice=api.post('/api/voice/text',json={'request_id':key,'source':'synthetic','text':text}).json()['id']
                    r=api.post('/api/llm/requests',json={'request_id':key,'voice_id':voice,'mode':'auto','expected_text_sha256':sha(text)})
                    assert r.status_code==202,r.text
                    return r.json()
                secret=note('비공개 카탈로그','PRIVATE_BODY_SENTINEL',False)
                public=note('합성 포장','<script>window.CATALOG_PWNED=true</script>literal body',True)
                read=submit('합성 포장 메모 읽어줘')
                check('named read returns selected ID via real MemoAdapter',read['assistant']['tool_result']['note_id']==public)
                check('no model transport for named read',not read['dispatch_attempted'] and not model.calls)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000})
                        page.on('pageerror',lambda e:results['page_errors'].append(str(e)))
                        load_bridged(page,api,'manager');page.wait_for_selector('.kpis')
                        page.evaluate("RoomManager.navigate('llm')")
                        page.wait_for_selector('.llm-output')
                        check('stored HTML body is escaped not executed',page.evaluate('window.CATALOG_PWNED') is None and '<script>' in page.locator('.llm-output').inner_text())
                        private=submit('비공개 카탈로그 메모 읽어줘')
                        projected=api.get('/api/display/llm/'+private['id']).json()
                        check('private title and body absent from display projection','PRIVATE_BODY_SENTINEL' not in str(projected) and '비공개 카탈로그' not in str(projected))
                        pending=submit('합성 포장 메모 비워줘')
                        check('named clear stops at existing confirmation',pending['status']=='awaiting_confirmation')
                        page.evaluate('RoomLLM.refresh()')
                        page.wait_for_selector(f'[data-llm=select][data-id="{pending["id"]}"]')
                        page.locator(f'[data-llm=select][data-id="{pending["id"]}"]').click()
                        page.wait_for_selector('[data-llm=confirm-action]')
                        check('preview shows actual selected title','합성 포장' in page.locator('[data-widget-preview]').inner_text())
                        check('body unchanged before confirmation',next(n for n in app.state.life.notes() if n['id']==public)['body'].endswith('literal body'))
                        page.locator('[data-llm=confirm-action]').click()
                        page.wait_for_selector('[data-agent-state=succeeded]')
                        check('existing confirmation clears only selected body',next(n for n in app.state.life.notes() if n['id']==public)['body']=='')
                        check('other private note unchanged',next(n for n in app.state.life.notes() if n['id']==secret)['body']=='PRIVATE_BODY_SENTINEL')
                        note('합성 포장','OTHER',False)
                        conflict=submit('합성 포장 메모 읽어줘')
                        check('duplicate names require clarification',conflict['status']=='needs_clarification')
                        check('no model calls across tested flows',not model.calls)
                        check('no uncaught browser errors',not results['page_errors'])
                    finally:browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10)
    results['passed']=len(results['checks'])
    path=ROOT/'artifacts/test-results/memo-catalog-browser.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print('TOTAL',results['passed'])


if __name__=='__main__':main()
