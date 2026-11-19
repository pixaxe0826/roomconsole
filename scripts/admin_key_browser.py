"""Real manager settings UI in Chromium with synthetic auth; no live keys or network."""

import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
MOCK = r"""() => {
  window.__writes = [];
  window.__keyFailure = null;
  window.__loggedIn = true;
  const state = {
    today: '2026-11-20', revision: 1, tasks: [], widgets: [],
    layout: {columns: 8, rows: 6, widgets: [], version: 1},
    settings: {title:'Synthetic Room', timezone:'Asia/Seoul', theme:'light',
      location_name:'', latitude:null, longitude:null, weather_interval_minutes:15},
  };
  const overview = {devices:[],events:[],voice:[],stats:{connected:0,tasks:0}};
  window.WebSocket = class {
    constructor() {this.readyState=1;setTimeout(()=>this.onopen?.(),0)}
    send() {}
    close() {this.readyState=3;this.onclose?.({code:1000})}
  };
  window.fetch = async (url, options={}) => {
    const path=new URL(url,location.href).pathname,method=options.method||'GET';
    const reply=(value,status=200)=>new Response(JSON.stringify(value),{
      status,headers:{'Content-Type':'application/json'},
    });
    if(path==='/api/auth/me')return window.__loggedIn?reply({role:'admin',device_id:null}):reply({},401);
    if(path==='/api/state')return reply(state);
    if(path==='/api/admin/overview')return reply(overview);
    if(path==='/api/auth/key'&&method==='POST'){
      const body=JSON.parse(options.body);
      window.__writes.push({method,path,body,headers:options.headers,credentials:options.credentials});
      if(window.__keyFailure)return reply(window.__keyFailure.body,window.__keyFailure.status);
      if(body.current_key!=='synthetic-old-key')return reply({detail:'현재 관리자 키를 확인하세요.'},401);
      if(body.new_key.length<8)return reply({detail:'짧은 키'},422);
      if(body.new_key!==body.confirm_key)return reply({detail:'확인 불일치'},422);
      window.__loggedIn=false;
      return reply({ok:true,relogin_required:true});
    }
    throw Error('Unexpected synthetic request: '+method+' '+path);
  };
}"""


def run():
    checks = []

    def check(name, condition):
        assert condition, name
        checks.append(name)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome'),
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def local_assets(route):
            path = urlparse(route.request.url).path
            if path == '/manager':
                route.fulfill(path=str(ROOT / 'web/manager.html'), content_type='text/html; charset=utf-8')
            elif path.startswith('/static/'):
                file = ROOT / 'web' / path.removeprefix('/static/')
                if file.is_file() and file.resolve().parent == (ROOT / 'web').resolve():
                    route.fulfill(path=str(file))
                else:
                    route.fulfill(status=404)
            else:
                route.fulfill(status=404)

        page.route('**/*', local_assets)
        page.add_init_script('(' + MOCK + ')()')
        page.goto('https://room-hub.test/manager#settings')
        page.wait_for_selector('#adminKeyForm')
        check('Settings exposes current, new and confirmation secret fields',
              page.locator('#adminKeyForm input[type=password]').count() == 3)
        check('Both new password fields enforce eight characters in HTML',
              page.locator('#adminKeyForm [name=new_key]').get_attribute('minlength') == '8' and
              page.locator('#adminKeyForm [name=confirm_key]').get_attribute('minlength') == '8')
        check('Credentials are never rendered as prefilled values',
              all(not page.locator(f'#adminKeyForm [name={field}]').input_value()
                  for field in ('current_key', 'new_key', 'confirm_key')))
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 900})
            dimensions = page.evaluate('''() => ({
              fields:[...document.querySelectorAll('#adminKeyForm input')].map(el => ({
                width:Math.round(el.getBoundingClientRect().width),
                right:Math.round(el.getBoundingClientRect().right),
              })),
              documentWidth:document.documentElement.scrollWidth,
              viewportWidth:innerWidth,
            })''')
            check(f'Form fields stay visible and do not overflow at {width}px: {dimensions}',
                  all(field['width'] >= 140 and field['right'] <= width
                      for field in dimensions['fields']) and
                  dimensions['documentWidth'] <= width)

        current = page.locator('#adminKeyForm [name=current_key]')
        new = page.locator('#adminKeyForm [name=new_key]')
        confirmation = page.locator('#adminKeyForm [name=confirm_key]')
        submit = page.locator('#adminKeyForm [type=submit]')
        current.fill('synthetic-old-key')
        new.fill('1234567')
        confirmation.fill('1234567')
        submit.click()
        check('Native min-length validation blocks a seven-character value',
              not page.evaluate('window.__writes.length') and
              not new.evaluate('(el)=>el.validity.valid'))
        new.fill('new!key8')
        confirmation.fill('mismatch')
        submit.click()
        page.wait_for_function("document.querySelector('#adminKeyError').textContent.includes('일치하지')")
        check('Mismatched confirmation never sends a request', not page.evaluate('window.__writes.length'))

        confirmation.fill('new!key8')
        page.evaluate("window.__keyFailure={status:409,body:{detail:'환경 변수 HUB_ADMIN_TOKEN으로 관리 중입니다.'}}")
        submit.click()
        page.wait_for_function("document.querySelector('#adminKeyError').textContent.includes('HUB_ADMIN_TOKEN')")
        check('Environment-managed conflict leaves the entered fields available for correction',
              current.input_value() == 'synthetic-old-key' and
              new.input_value() == 'new!key8' and
              not page.locator('#managerApp').evaluate('(el)=>el.classList.contains("hidden")'))
        check('Authenticated same-origin request includes CSRF header and no extra privilege fields',
              page.evaluate('''() => window.__writes.length === 1 &&
                window.__writes[0].method === 'POST' &&
                window.__writes[0].path === '/api/auth/key' &&
                window.__writes[0].credentials === 'same-origin' &&
                window.__writes[0].headers['X-Room-Request'] === '1' &&
                Object.keys(window.__writes[0].body).sort().join(',') ===
                'confirm_key,current_key,new_key' '''))

        page.evaluate('window.__keyFailure=null')
        submit.click()
        page.wait_for_selector('#login:not(.hidden)')
        check('Successful rotation logs out immediately and requests new sign-in',
              page.locator('#managerApp').evaluate('(el)=>el.classList.contains("hidden")') and
              '새 키로 다시 로그인' in page.locator('.toast').inner_text())
        check('Password inputs are cleared after success',
              all(not page.locator(f'#adminKeyForm [name={field}]').input_value()
                  for field in ('current_key', 'new_key', 'confirm_key')))
        check('No uncaught JavaScript exceptions', not errors)
        browser.close()

    folder = ROOT / 'artifacts/test-results'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'admin-key-browser.json').write_text(json.dumps({
        'checks': checks, 'passed': len(checks),
        'scope': 'Production manager HTML/CSS/JS with synthetic admin API, no real credentials/network.',
    }, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'PASS {len(checks)} admin key browser checks (synthetic)')


if __name__ == '__main__':
    run()
