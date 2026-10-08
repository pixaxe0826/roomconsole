"""Chromium runtime UI with synthetic API; never real inference/LAN/Safari.

Default: intercepted HTTPS with CSP. HUB_BROWSER_BRIDGE=1 is an explicit
about:blank/fetch-binding mode for environments that block browser navigation;
that mode does not test the browser's HTTP/CSP enforcement.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run():
    checks = []
    def check(name, condition):
        assert condition, name
        checks.append(name)
        print('PASS', name, flush=True)
    bridged = os.getenv('HUB_BROWSER_BRIDGE') == '1'
    report = {'scope': __doc__, 'transport': 'fetch_binding_no_http_csp' if bridged else 'intercepted_https_with_csp',
              'checks': checks}
    state = {'speech': {'state': 'unknown', 'backend': 'remote_http', 'configured_model': 'medium',
             'reported_model': None, 'configured_device': 'cuda', 'reported_device': None,
             'identity_note': '설정값은 실행 증명이 아닙니다.'},
             'llm': {'enabled': True, 'state': 'unknown', 'configured_model': 'synthetic-1.7B',
             'reported_models': [], 'reported_device': None, 'model_list_match': False}}
    calls = []; errors = []

    def api(path, method):
        calls.append((method, path))
        if path == '/healthz':
            return {'status': 200, 'data': {'status': 'ok'}}
        if path in ('/api/ai/status', '/api/ai/probe'):
            if method == 'POST':
                state['speech'].update(state='online', last_probe={'checked_at': '2026-01-02T03:04:05Z'})
                state['llm'].update(state='online', model_list_match=True,
                                    reported_models=['<img src=x onerror="window.BAD=true">'])
            return {'status': 200, 'data': copy.deepcopy(state)}
        if path == '/api/speech/status':
            return {'status': 200, 'data': {'backend': 'remote_http', 'model': 'medium', 'language': 'ko',
                                           'threads': None, 'max_seconds': 30}}
        return {'status': 404, 'data': {}}

    # The actual manager document declares UTF-8. Mirror that in the reduced
    # fixture so Korean text from external scripts is decoded correctly.
    summary = '<meta charset="utf-8"><div class="llm-connection-tools"></div><div class="speech-engine"><strong>base</strong><span>CPU null스레드</span></div>'
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome'), args=['--no-sandbox'])
        def open_page(summary_only=False):
            page = browser.new_page(viewport={'width': 1024, 'height': 900})
            page.on('pageerror', lambda e: errors.append(str(e)))
            script = 'runtime-summary.js' if summary_only else 'runtime.js'
            html = summary if summary_only else (ROOT/'web/runtime.html').read_text('utf-8')
            if bridged:
                page.expose_function('runtimeTestFetch', api)
                page.set_content(re.sub(r'<script\b[^>]*>.*?</script>|<link\b[^>]*>', '', html, flags=re.S))
                page.evaluate("""() => { window.fetch = async (url, options={}) => {
                    const value = await window.runtimeTestFetch(new URL(url,'https://roomhub.test').pathname,options.method||'GET');
                    return new Response(JSON.stringify(value.data),{status:value.status,headers:{'Content-Type':'application/json'}});
                }; }""")
                page.add_style_tag(content=(ROOT/'web/runtime.css').read_text('utf-8'))
                page.add_script_tag(content=(ROOT/'web'/script).read_text('utf-8'))
            else:
                def serve(route):
                    req = route.request; path = urlsplit(req.url).path
                    if path == '/manager/runtime':
                        body = html if not summary_only else summary + '<script defer src="/static/runtime-summary.js"></script>'
                        route.fulfill(body=body, content_type='text/html; charset=utf-8', headers={
                            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'"})
                    elif path.startswith('/static/'):
                        asset = ROOT/'web'/path.split('/')[-1]
                        route.fulfill(body=asset.read_text('utf-8'), content_type='text/javascript; charset=utf-8' if asset.suffix=='.js' else 'text/css; charset=utf-8')
                    else:
                        result = api(path, req.method)
                        route.fulfill(status=result['status'], json=result['data'])
                page.route('**/*', serve)
                page.goto('https://roomhub.test/manager/runtime')
            return page
        try:
            # Playwright wait_for_function() uses page eval while polling; the real
            # strict CSP intentionally forbids unsafe-eval. Locator assertions
            # poll DOM text without weakening the application's CSP.
            page = open_page()
            expect(page.locator('#core')).to_contain_text('정상')
            check('initial GET has no outbound probe trigger', not any(m=='POST' for m,_ in calls))
            check('unknown device clearly labelled', '미확인' in page.locator('#speech').inner_text())
            page.locator('#probe').click()
            expect(page.locator('#llm')).to_contain_text('최근 연결 확인 성공')
            check('probe is one explicit POST', sum((m,path)==('POST','/api/ai/probe') for m,path in calls)==1)
            check('untrusted model names rendered only as text', page.locator('#llm img').count()==0 and not page.evaluate('!!window.BAD'))
            check('configured CUDA is not reported CUDA', page.locator('#speech dd').nth(5).inner_text()=='미확인')
            state['speech']['state']='unknown'; state['llm']['state']='unknown'
            expect(page.locator('#speech')).to_contain_text('확인 만료', timeout=8000)
            check('polling expires status without re-probing', sum(m=='POST' for m,_ in calls)==1)
            for width,height in [(1024,900), (390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                check(f'no horizontal overflow {width}', not page.evaluate('document.documentElement.scrollWidth>innerWidth'))
            shots=ROOT/'artifacts/screenshots'; shots.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(shots/'runtime-m340.png'), full_page=True)
            page.close()
            # Separate document: no interval/CSP left over from the diagnostics page.
            page = open_page(summary_only=True)
            expect(page.locator('.speech-engine span')).to_contain_text('원격')
            check('legacy CPU label replaced for remote only', 'CPU' not in page.locator('.speech-engine').inner_text())
            check('manager has one diagnostics link', page.locator('[data-runtime-link]').count()==1)
            before=sum(path=='/api/speech/status' for _,path in calls)
            page.wait_for_timeout(200)
            check('observer does not create a fetch loop', sum(path=='/api/speech/status' for _,path in calls)==before==1)
            check('no uncaught browser errors', not errors)
        finally:
            browser.close()
    output=ROOT/'artifacts/test-results'; output.mkdir(parents=True,exist_ok=True)
    report.update(passed=len(checks), page_errors=errors)
    (output/'runtime-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding='utf-8')
    print('TOTAL', len(checks))


if __name__=='__main__':
    run()
