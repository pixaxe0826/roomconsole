"""Production food editor/widget + synthetic HTTP in Chromium, not a live device test."""
import json
import os
from pathlib import Path
import shutil

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

MOCK = r"""() => {
  const timestamp='2026-11-16T03:00:00+00:00';
  window.__food = [{id:'sample-food',name:'합성 우유',quantity:2,expiry_date:'2026-11-19',version:1,created_at:timestamp,updated_at:timestamp}];
  window.__foodCalls = []; window.__foodFailure = null;
  window.__foodClock = '2026-11-16';
  window.RoomManager = {getState:()=>({today:window.__foodClock,settings:{timezone:'Asia/Seoul'}}),refresh:async()=>{}};
  window.fetch = async (url, options={}) => {
    const method=options.method||'GET', body=options.body?JSON.parse(options.body):null;
    window.__foodCalls.push({url,method,body,headers:options.headers,credentials:options.credentials});
    const reply=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
    if(!/^\/api\/food\/items(?:\/[^/?]+(?:\?version=\d+)?)?$/.test(url)) throw Error('Unexpected request: '+url);
    if(window.__foodFailure==='401') return reply({},401);
    if(method==='GET' && url==='/api/food/items') return reply({enabled:false,items:structuredClone(window.__food),today:window.__foodClock,timezone:'Asia/Seoul',due_soon_days:3});
    if(window.__foodFailure==='abort') throw new DOMException('synthetic timeout','AbortError');
    if(window.__foodFailure==='422') return reply({detail:[{msg:'합성 입력 오류'}]},422);
    if(method==='POST' && url==='/api/food/items') {const row={name:body.name,quantity:body.quantity,expiry_date:body.expiry_date,id:'created-food',version:1,created_at:timestamp,updated_at:timestamp};window.__food.push(row);return reply({...row,duplicate:false},201);}
    const id=decodeURIComponent(url.split('/').pop().split('?')[0]),row=window.__food.find(x=>x.id===id);
    if(!row) return reply({detail:'항목 없음'},404);
    if(method==='PUT'){if(body.version!==row.version)return reply({detail:'다른 화면에서 수정됨'},409);Object.assign(row,body,{version:row.version+1});return reply(row);}
    if(method==='DELETE'){const v=Number(new URL(url,'https://example.invalid').searchParams.get('version'));if(v!==row.version)return reply({detail:'버전 충돌'},409);window.__food=window.__food.filter(x=>x.id!==id);return reply({id,deleted:true,duplicate:false});}
    throw Error('Unexpected method: '+method);
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

        def wait_editor_idle():
            # A cleared draft acknowledges the mutation but precedes its GET refresh.
            # Wait for finally/setBusy, especially before changing the next mock response.
            page.wait_for_function("document.querySelector('form[data-food-editor-form]')?.getAttribute('aria-busy') === 'false'")

        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        page.set_content('<!doctype html><html lang="ko"><meta charset="utf-8"><body class="manager-body"><main id="foodRoot" style="padding:18px"></main></body></html>')
        for name in ('base.css', 'manager.css', 'food-expiry.css'):
            page.add_style_tag(content=(ROOT / 'web' / name).read_text(encoding='utf-8'))
        page.add_script_tag(content=(ROOT / 'web/shared.js').read_text(encoding='utf-8'))
        page.evaluate(MOCK)
        page.add_script_tag(content=(ROOT / 'web/food-expiry.js').read_text(encoding='utf-8'))
        page.evaluate("RoomFood.mount(document.getElementById('foodRoot'))")
        page.wait_for_selector('[data-food-op=edit]')
        check('Manager table has the requested column order', page.locator('.food-manager-table thead th').all_text_contents() == ['식품', '개수', '소비기한'])
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 900})
            dimensions = page.evaluate("({overflow:document.documentElement.scrollWidth>innerWidth,quantity:document.querySelector('[name=quantity]').getBoundingClientRect().width,date:document.querySelector('[name=expiry_date]').getBoundingClientRect().width})")
            check(f'Legible inputs and no page overflow at {width}px', not dimensions['overflow'] and dimensions['quantity'] >= 100 and dimensions['date'] >= 150)
        page.locator('[name=name]').fill('<b>합성 두부</b>')
        page.locator('[name=quantity]').fill('4')
        page.locator('[name=expiry_date]').fill('2026-11-18')
        page.evaluate('RoomFood.refresh()')
        check('Polling preserves active draft', page.locator('[name=name]').input_value() == '<b>합성 두부</b>')
        page.evaluate("window.__foodFailure='422'")
        page.locator('[type=submit]').click()
        page.wait_for_function("document.querySelector('[data-food-feedback]').textContent.includes('합성 입력 오류')")
        wait_editor_idle()
        check('Validation failures keep all entered values', page.locator('[name=quantity]').input_value() == '4' and page.locator('[name=expiry_date]').input_value() == '2026-11-18')
        page.evaluate("window.__foodFailure='abort'")
        page.locator('[type=submit]').click()
        page.wait_for_function("document.querySelector('[data-food-feedback]').textContent.includes('저장 결과를 확인하지 못했습니다')")
        wait_editor_idle()
        page.evaluate('window.__foodFailure=null')
        page.locator('[type=submit]').click()
        page.wait_for_function("window.__food.length===2 && document.querySelector('[name=name]').value===''")
        wait_editor_idle()
        writes = page.evaluate("window.__foodCalls.filter(x=>x.method==='POST')")
        check('Explicit retries reuse one request ID for unchanged data', len(writes) == 3 and len({x['body']['request_id'] for x in writes}) == 1)
        check('Manual writes use authenticated same-origin CSRF headers', all(x['credentials'] == 'same-origin' and x['headers']['X-Room-Request'] == '1' for x in writes))
        check('Numbers and dates sent without extra authority', writes[-1]['body']['quantity'] == 4 and writes[-1]['body']['expiry_date'] == '2026-11-18' and set(writes[-1]['body']) == {'request_id', 'name', 'quantity', 'expiry_date'})
        page.wait_for_selector('[data-food-op=edit][data-id=created-food]')
        check('Untrusted food names render as escaped text', page.locator('.food-manager-table b').count() == 0 and '<b>합성 두부</b>' in page.locator('.food-manager-table').inner_text())
        page.locator('[data-food-op=edit][data-id=sample-food]').click()
        page.locator('[name=quantity]').fill('9')
        page.evaluate("window.__food[0].version=2;window.__food[0].quantity=5;RoomFood.refresh()")
        page.wait_for_selector('[data-food-conflict]:not(.hidden)')
        check('Version refresh preserves draft and blocks stale save/delete', page.locator('[name=quantity]').input_value() == '9' and page.locator('[type=submit]').is_disabled() and page.locator('[data-food-op=delete]').is_disabled())
        page.locator('[data-food-op=reload-item]').click()
        page.wait_for_function("document.querySelector('[name=quantity]').value==='5'")
        page.locator('[name=quantity]').fill('6')
        page.evaluate('window.__food[0].version=3')
        page.locator('[type=submit]').click()
        page.wait_for_function("document.querySelector('[data-food-feedback]').textContent.includes('다른 화면에서 수정됨')")
        wait_editor_idle()
        check('HTTP 409 does not overwrite the draft or silently retry', page.locator('[name=quantity]').input_value() == '6' and page.locator('[type=submit]').is_disabled() and page.evaluate("window.__foodCalls.filter(x=>x.method==='PUT').length") == 1)
        page.locator('[data-food-op=reload-item]').click()
        page.wait_for_function("document.querySelector('[name=quantity]').value==='5'")
        page.locator('[name=quantity]').fill('6')
        page.locator('[type=submit]').click()
        page.wait_for_function('window.__food[0].quantity===6')
        wait_editor_idle()
        check('Explicit reload binds update to the latest version', page.evaluate("window.__foodCalls.filter(x=>x.method==='PUT').at(-1).body.version") == 3)
        page.wait_for_function("document.querySelector('[name=name]').value===''")
        page.locator('[data-food-op=edit][data-id=sample-food]').click()
        page.locator('[data-food-op=delete]').click()
        page.wait_for_function('window.__food.length===1')
        wait_editor_idle()
        check('Deletion is explicit and versioned', page.evaluate("window.__foodCalls.filter(x=>x.method==='DELETE').at(-1).url") == '/api/food/items/sample-food?version=4')
        check('All mutations retain same-origin authentication and CSRF headers', page.evaluate("window.__foodCalls.filter(x=>x.method!=='GET').every(x=>x.credentials==='same-origin'&&x.headers['X-Room-Request']==='1')"))
        page.wait_for_function("document.querySelector('[name=name]').value===''")
        page.evaluate("async () => { window.__foodFailure='401'; await RoomFood.refresh(); }")
        page.wait_for_function("document.querySelector('#foodRoot').children.length===0")
        check('Authentication expiry clears food data and drafts', page.locator('#foodRoot').inner_text() == '')
        check('No hidden model/audio requests', page.evaluate("window.__foodCalls.every(x=>x.url.startsWith('/api/food/items'))"))

        # Exact display widget source, fixed synthetic server date. No editor in this page.
        widget = browser.new_page(viewport={'width': 390, 'height': 800})
        widget.on('pageerror', lambda error: errors.append(str(error)))
        # The production flex wrapper constrains .widget-content and its scroll area.
        widget.set_content('<!doctype html><html lang="ko"><meta charset="utf-8"><body><main class="widget" id="widgetRoot" style="height:700px;width:100%;background:var(--surface)"></main></body></html>')
        for name in ('web/base.css', 'web/client.css', 'widgets/food-expiry/style.css'):
            widget.add_style_tag(content=(ROOT / name).read_text(encoding='utf-8'))
        widget.add_script_tag(content=(ROOT / 'web/shared.js').read_text(encoding='utf-8'))
        source = (ROOT / 'widgets/food-expiry/widget.js').read_text(encoding='utf-8').replace('export function', 'function')
        widget.add_script_tag(content=source + '\nwindow.FoodWidget={render,bind};')
        widget.evaluate("""() => {
          window.__ctx={state:{today:'2026-11-16',food:{enabled:true,items:[{id:'expired',name:'<script>BAD()</script>',quantity:2,expiry_date:'2026-11-15'},...Array.from({length:123},(_,i)=>({id:'item-'+i,name:'합성 식품 '+i,quantity:i+1,expiry_date:'2026-11-19'}))]}},instance:{title:'소비기한 알리미'},viewState:{},expanded:true,connected:true,util:Room,expand(){}};
          let cleanup;
          const paint=()=>{cleanup?.();const root=document.getElementById('widgetRoot');root.innerHTML=FoodWidget.render(window.__ctx);cleanup=FoodWidget.bind(root,window.__ctx);};
          window.__ctx.redraw=paint;paint();
        }""")
        check('Display table has the requested column order', widget.locator('.food-expiry-table thead th').all_text_contents() == ['식품', '개수', '소비기한'])
        check('Widget does not expose editing fields', widget.locator('input,select,textarea,[contenteditable=true]').count() == 0)
        check('Display strings are escaped and dates have semantic markup', widget.locator('#widgetRoot script').count() == 0 and widget.locator('time[datetime="2026-11-15"]').count() == 1)
        check('Overdue and imminent labels are readable', '기한 1일 지남' in widget.locator('#widgetRoot').inner_text() and '3일 남음' in widget.locator('#widgetRoot').inner_text())
        for _ in range(2):
            widget.locator('[data-food-more]').click()
        check('Pagination reaches every food without hidden truncation', widget.locator('tbody tr').count() == 124 and widget.locator('[data-food-more]').count() == 0)
        check('Expanded portrait widget stays inside viewport', not widget.evaluate('document.documentElement.scrollWidth>innerWidth'))
        widget.set_viewport_size({'width': 834, 'height': 1194})
        widget.evaluate("window.__ctx.expanded=false;document.getElementById('widgetRoot').style.cssText='height:260px;width:390px;background:var(--surface)';window.__ctx.redraw()")
        check('4x2-sized table has a usable scroll region', widget.locator('.food-expiry-scroll').evaluate('(el)=>el.clientHeight>80&&el.scrollHeight>el.clientHeight'))
        check('No uncaught JavaScript exceptions', not errors)
        shots = ROOT / 'artifacts/screenshots'; shots.mkdir(parents=True, exist_ok=True)
        widget.screenshot(path=str(shots / 'food-expiry-widget.png'))
        browser.close()
    reports = ROOT / 'artifacts/test-results'; reports.mkdir(parents=True, exist_ok=True)
    (reports / 'food-expiry-browser.json').write_text(json.dumps({'passed':len(checks),'checks':checks,
        'scope':'Chromium production CSS/JS + synthetic HTTP; no real API, microphone, SQLite or iPad.'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'PASS {len(checks)} food browser checks (synthetic; no paid calls)')


if __name__ == '__main__':
    run()
