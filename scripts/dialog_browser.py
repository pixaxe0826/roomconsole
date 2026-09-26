"""M3.3-A production manager UI over real HTTP/SQLite, synthetic sources only.

Uses assistant_browser's explicit fetch bridge and injected invalidations. This
is not physical V35/Safari/ASR/Qwen testing and does not use a real user database.
"""
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright

from assistant_browser import ROOT, load_bridged
from app.main import create_app


class NoModel:
    def __init__(self):
        self.calls = []

    async def generate(self, *args, **kwargs):
        self.calls.append(True)
        raise AssertionError('Typed dialog must not invoke a model')


def main():
    results = {'scope': __doc__, 'checks': [], 'page_errors': []}

    def check(name, condition):
        assert condition, name
        results['checks'].append(name)
        print('PASS', name, flush=True)

    with tempfile.TemporaryDirectory() as td:
        model = NoModel()
        app = create_app(Path(td) / 'data', weather_enabled=False, llm_backend=model)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                              headers={'Authorization': 'Bearer ' + app.state.admin_token,
                                       'X-Room-Request': '1'}) as api:
                voices = []
                for index, text in enumerate(['내일 할 일 추가해', '내일 알람 맞춰줘', '할 일 추가해']):
                    response = api.post('/api/voice/text', json={
                        'request_id': f'dialog-browser-source-{index}', 'source': 'synthetic-dialog', 'text': text})
                    assert response.status_code == 202, response.text
                    voices.append(response.json()['id'])
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
                    try:
                        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                        page.on('pageerror', lambda e: results['page_errors'].append(str(e)))
                        load_bridged(page, api, 'manager')
                        page.add_script_tag(content=(ROOT / 'web/manager-dialog.js').read_text(encoding='utf-8'))
                        page.wait_for_selector('.kpis')

                        def open_voice(index):
                            page.evaluate("RoomManager.navigate('voice')")
                            page.wait_for_selector('#voiceLLMMode')
                            page.locator('#voiceLLMMode').select_option('auto')
                            page.locator(f'[data-op=voice-to-llm][data-id="{voices[index]}"]').click()
                            page.wait_for_selector('[data-dialog-input]')

                        open_voice(0)
                        check('missing title has an explicit selected-request input',
                              page.locator('[data-dialog-input]').count() == 1)
                        root_id = page.evaluate('RoomLLM.getSelection()')
                        page.locator('[data-dialog-input]').fill('합성 브라우저 포장')
                        page.wait_for_timeout(3600)
                        check('unsent dialog draft survives periodic invalidation',
                              page.locator('[data-dialog-input]').input_value() == '합성 브라우저 포장')
                        page.locator('[data-dialog-send]').click()
                        page.wait_for_selector('[data-agent-state=awaiting_confirmation]')
                        child_id = page.evaluate('RoomLLM.getSelection()')
                        page.wait_for_selector(f'[data-dialog-panel][data-dialog-observed="{child_id}"]')
                        child = api.get('/api/llm/requests/' + child_id).json()
                        check('reply is a different request bound to exact parent',
                              child_id != root_id and child['parent_id'] == root_id)
                        check('title reply is not a fabricated full source utterance',
                              child['source_text'] == '합성 브라우저 포장')
                        check('filled dialog does not write before manager confirmation',
                              not api.get('/api/state').json()['tasks'])
                        check('generic retry is hidden for source-proof child',
                              page.locator('#llmDetail [data-llm=retry]:visible').count() == 0)
                        page.locator('[data-llm=confirm-action]').click()
                        page.wait_for_selector('[data-agent-state=succeeded]')
                        check('existing confirmation creates exactly one requested task',
                              [t['title'] for t in api.get('/api/state').json()['tasks']] == ['합성 브라우저 포장'])

                        open_voice(1)
                        page.locator('[data-dialog-input]').fill('3시')
                        page.locator('[data-dialog-send]').click()
                        page.wait_for_function("() => document.querySelector('[data-dialog-input]') && !document.querySelector('[data-dialog-input]').value")
                        selected = api.get('/api/llm/requests/' + page.evaluate('RoomLLM.getSelection()')).json()
                        check('ambiguous clock asks again and keeps the date',
                              selected['dialog']['awaiting_slot'] == 'time' and
                              selected['dialog']['known_slots']['date'] is not None and
                              selected['dialog']['known_slots']['time'] is None)
                        page.locator('[data-dialog-cancel]').click()
                        page.wait_for_function("() => !document.querySelector('[data-dialog-input]')")
                        selected = api.get('/api/llm/requests/' + page.evaluate('RoomLLM.getSelection()')).json()
                        check('dialog cancel terminates only this pending input',
                              selected['status'] == 'cancelled' and selected['dialog']['phase'] == 'cancelled')
                        check('cancel makes no alarm and preserves existing task',
                              not app.state.life.alarms() and len(api.get('/api/state').json()['tasks']) == 1)
                        check('all dialog operations called model zero times', not model.calls)
                        check('no uncaught manager browser errors', not results['page_errors'])
                        output = ROOT / 'artifacts/screenshots'
                        output.mkdir(parents=True, exist_ok=True)
                        page.evaluate("document.querySelector('#llmTopNotice').textContent='M3.3 합성 대화 검사 · 실제 사용자 데이터/모델 아님'")
                        page.screenshot(path=str(output / 'dialog_manager.png'), full_page=True)
                    finally:
                        browser.close()
        finally:
            server.should_exit = True
            worker.join(timeout=10)
    results['passed'] = len(results['checks'])
    output = ROOT / 'artifacts/test-results/dialog-browser.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print('TOTAL', results['passed'])


if __name__ == '__main__':
    main()
