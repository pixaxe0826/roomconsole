"""Synthetic read-only query UI via real HTTP/SQLite + explicit browser bridge.

The existing bridge injects WS notifications. Not physical V35/iPad/Qwen.
"""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('HUB_DATA_DIR', str(ROOT / 'artifacts/runtime-data'))
from assistant_browser import load_bridged
from app.main import create_app
from app.llm import sha


class NoModel:
    calls = 0
    async def generate(self, *args):
        self.calls += 1
        raise AssertionError('A grounded read must not call an LLM')


def main():
    result = {'scope': __doc__, 'checks': [], 'page_errors': []}
    def check(name, value):
        assert value, name
        result['checks'].append(name)
    with tempfile.TemporaryDirectory() as td:
        model = NoModel()
        app = create_app(Path(td) / 'data', weather_enabled=False, llm_backend=model)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                              headers={'Authorization': 'Bearer '+app.state.admin_token, 'X-Room-Request': '1'}) as api:
                state = api.get('/api/state').json(); layout = state['layout']; layout['rows'] = 8
                layout['widgets'].append({'id':'memo-test','type':'note','title':'메모','x':0,'y':6,'w':4,'h':2,'config':{}})
                assert api.put('/api/layout', json=layout).status_code == 200
                assert api.post('/api/life/notes', json={'request_id':'browser-fast-note','title':'합성 데이터',
                    'body':'합성 메모 <script>window.PWNED=true</script>', 'shared':True}).status_code == 201
                assert api.post('/api/tasks', json={'title':'합성 오후 회의','date':state['today'],'time':'13:00'}).status_code == 201
                records=[]
                for n, (text,intent) in enumerate([
                    ('현재 메모에 남아있는 내용 읽어줘.','MEMO_READ'),
                    ('오늘 할 일 확인해줘.','TODO_LIST'),
                    ('오늘 오후 일정 확인해줘.','CALENDAR_QUERY'),
                ]):
                    v=api.post('/api/voice/text',json={'request_id':f'fast-voice-{n}','source':'test-only','text':text}).json()
                    d=api.post('/api/llm/requests',json={'request_id':f'fast-browser-{n}','voice_id':v['id'],
                        'expected_text_sha256':sha(text),'mode':'auto'}).json()
                    check(intent+' is immediately complete',d['status']=='succeeded' and not d['dispatch_attempted'])
                    records.append((d,intent))
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1100})
                        page.on('pageerror',lambda e: result['page_errors'].append(str(e)))
                        load_bridged(page,api,'manager');page.wait_for_selector('.kpis')
                        page.evaluate("RoomManager.navigate('llm')")
                        for d,intent in records:
                            page.locator(f'[data-llm=select][data-id="{d["id"]}"]').click()
                            panel=page.locator('[data-routing]').filter(has_text=intent)
                            panel.wait_for()
                            check(intent+' route and source shown','FAST_PATH' in panel.inner_text() and 'room_hub_sqlite' in panel.inner_text())
                            check(intent+' model marked N/A','0초 · 토큰 N/A' in panel.inner_text())
                            check(intent+' exact stored text rendered',page.locator('.llm-output').inner_text()==d['response_json']['output'])
                        check('memo HTML never executed',page.evaluate('window.PWNED!==true'))
                        check('model never called',model.calls==0)
                        check('no browser exceptions',not result['page_errors'])
                        shots=ROOT/'artifacts/screenshots';shots.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(shots/'fast-read-diagnostics.png'),full_page=True)
                    finally: browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10)
    result['passed']=len(result['checks'])
    out=ROOT/'artifacts/test-results';out.mkdir(parents=True,exist_ok=True)
    (out/'fast-reads-browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
