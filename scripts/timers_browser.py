"""Independent 2x2 timer cards: real API/SQLite, synthetic time; no physical audio claim."""
import json,os,shutil,socket,sys,tempfile,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx,uvicorn
from playwright.sync_api import sync_playwright,expect
from app.main import create_app


def main():
    out=ROOT/'artifacts/test-results';out.mkdir(parents=True,exist_ok=True)
    shots=ROOT/'artifacts/screenshots';shots.mkdir(parents=True,exist_ok=True)
    bridged=os.getenv('HUB_BROWSER_BRIDGE')=='1'
    report={'checks':[],'errors':[],'scope':('Python HTTP bridge / injected WS; ' if bridged else 'Chromium direct HTTP/WS; ')+'production UI + actual API/SQLite; synthetic time offset; V35/Safari/physical sound NOT_RUN'}
    def check(name,result):
        report['checks'].append({'name':name,'passed':bool(result)})
        print(('PASS ' if result else 'FAIL ')+name,flush=True);assert result,name
    with tempfile.TemporaryDirectory() as tmp:
        app=create_app(tmp,weather_enabled=False);offset=[0.];app.state.timers.clock=lambda:time.time()+offset[0]
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}';server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            assert server.started
            with httpx.Client(base_url=base,trust_env=False,headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
                layout=api.get('/api/state').json()['layout']
                layout['widgets']=[{'id':f'timer-{i}','type':'timers','title':title,'x':i*2,'y':0,'w':2,'h':2,'config':{}} for i,title in enumerate(('차 우리기','스트레칭'))]
                assert api.put('/api/layout',json=layout).status_code==200
                pair=api.post('/api/devices/pair',json={'name':'timer-browser-test'}).json()
                with sync_playwright() as p:
                    executable=os.getenv('CHROMIUM_PATH') or os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or shutil.which('chromium')
                    opts={'headless':True,'args':['--no-sandbox']}
                    if executable:opts['executable_path']=executable
                    browser=p.chromium.launch(**opts);page=browser.new_page(viewport={'width':1194,'height':834},has_touch=True)
                    page.on('pageerror',lambda e:report['errors'].append(str(e)))
                    if bridged:
                        from assistant_browser import load_bridged
                        display=httpx.Client(base_url=base,trust_env=False,headers={'X-Room-Request':'1'})
                        assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
                        load_bridged(page,display,'client')
                    else:page.goto(base+pair['path'])
                    page.locator('#grid .rh-timers').first.wait_for()
                    left=page.locator('#grid [data-widget-id="timer-0"]');right=page.locator('#grid [data-widget-id="timer-1"]')
                    def field(card,name):return card.locator(f'[data-timer-field="{name}"]')
                    def prepare(card,m,s):field(card,'minutes').fill(str(m));field(card,'seconds').fill(str(s))
                    def count(card):return card.locator('[data-timer-main]')
                    def status(card):return card.locator('[data-timer-status]')
                    def home_visible():return page.locator('#display').is_visible() and page.locator('#focus').is_hidden()
                    def fits(card):return card.locator('.rh-timers').evaluate('e=>e.scrollHeight<=e.clientHeight+1&&e.scrollWidth<=e.clientWidth+1')
                    check('Two independent 2x2 widgets rendered',page.locator('#grid .rh-timers').count()==2 and home_visible())
                    check('Minutes/seconds use 1-second granularity',field(left,'minutes').get_attribute('max')=='10' and field(left,'seconds').get_attribute('step')=='1' and field(left,'seconds').get_attribute('max')=='59')
                    prepare(left,4,0);prepare(right,1,30)
                    check('Independent drafts shown simultaneously',count(left).inner_text()=='4:00' and count(right).inner_text()=='1:30')
                    left.locator('button[type=submit]').click();expect(status(left)).to_have_text('실행 중',timeout=10000)
                    check('Start A leaves B idle and keeps its draft',status(right).inner_text()=='설정 시간' or status(right).inner_text()=='대기')
                    check('B draft survives A mutation redraw',field(right,'minutes').input_value()=='1' and field(right,'seconds').input_value()=='30')
                    right.locator('button[type=submit]').click();expect(status(right)).to_have_text('실행 중',timeout=10000)
                    timers={t['widget_id']:t for t in api.get('/api/timers').json()['items']}
                    a,b=timers['timer-0'],timers['timer-1']
                    check('240s and 90s attached to distinct widget IDs',a['duration_seconds']==240 and b['duration_seconds']==90 and a['id']!=b['id'])
                    check('Both countdowns visible on the same dashboard',home_visible() and left.locator('[data-timer-id]').get_attribute('data-timer-id')==a['id'] and right.locator('[data-timer-id]').get_attribute('data-timer-id')==b['id'])
                    check('Active widget cannot create a second hidden timer',left.locator('button[type=submit]').is_disabled() and field(left,'minutes').is_disabled())
                    check('1194x834 2x2 controls fit without scrolling',fits(left) and fits(right))
                    page.screenshot(path=str(shots/'timers-independent-1194.png'))
                    count(left).click();check('Touching countdown does not navigate to another screen',home_visible())
                    before=app.state.timers.get(b['id'])['deadline_at']
                    left.locator('[data-timer-stop]').click();expect(status(left)).to_have_text('종료됨')
                    check('Stop A keeps B running with unchanged deadline',status(right).inner_text()=='실행 중' and app.state.timers.get(b['id'])['deadline_at']==before and app.state.timers.get(b['id'])['state']=='running')
                    prepare(left,0,1);left.locator('button[type=submit]').click()
                    expect(status(left)).to_have_text('시간 종료',timeout=10000)
                    check('1-second expiry leaves B untouched',app.state.timers.get(b['id'])['state']=='running' and home_visible())
                    # Invalid input is rejected locally, not rounded or clamped.
                    before_count=len(api.get('/api/timers').json()['items'])
                    for m,s in ((0,0),(10,1),(0,60),(1,'1.5'),(-1,0)):
                        prepare(left,m,s);left.locator('button[type=submit]').click()
                        expect(left.locator('.timer-message')).to_contain_text('1초~10분')
                    check('0 / 601 / invalid seconds / decimal / negative rejected',len(api.get('/api/timers').json()['items'])==before_count)
                    prepare(left,10,0);left.locator('button[type=submit]').click();expect(status(left)).to_have_text('실행 중')
                    latest=next(t for t in api.get('/api/timers').json()['items'] if t['widget_id']=='timer-0' and t['state']=='running')
                    check('10 minutes is exactly 600 seconds',latest['duration_seconds']==600)
                    # Sound authorization belongs to each widget, not all instances.
                    check('Sound is initially opt-in per widget',page.evaluate("RoomTimers.soundStatus('timer-0')")=='소리 꺼짐' and page.evaluate("RoomTimers.soundStatus('timer-1')")=='소리 꺼짐')
                    left.locator('[data-timer-sound]').click()
                    check('Enabling A sound does not enable B',page.evaluate("RoomTimers.soundStatus('timer-1')")=='소리 꺼짐')
                    page.evaluate("RoomTimers.mute('timer-0')")
                    check('Mute A does not change timer state',app.state.timers.get(b['id'])['state']=='running')
                    offset[0]+=7
                    page.evaluate('RoomTimers.refresh(true)')
                    page.wait_for_timeout(250)
                    expected=app.state.timers.get(latest['id'])['remaining_seconds']
                    shown=count(left).inner_text();m,s=map(int,shown.split(':'))
                    check('Countdown follows fresh server deadline within 1s',abs(m*60+s-expected)<=1)
                    # Reload and stored per-instance duration; no duplicate start.
                    if bridged:load_bridged(page,display,'client')
                    else:page.reload()
                    expect(status(left)).to_have_text('실행 중');expect(status(right)).to_have_text('실행 중')
                    check('Reload preserves identity / duration / deadlines',field(left,'minutes').input_value()=='10' and field(right,'minutes').input_value()=='1' and field(right,'seconds').input_value()=='30' and app.state.timers.get(latest['id'])['deadline_at']==latest['deadline_at'])
                    for width,height in ((1024,768),(834,1194)):
                        page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(350)
                        check(f'{width}x{height} both 2x2 timers fit',fits(left) and fits(right) and home_visible())
                        page.screenshot(path=str(shots/f'timers-independent-{width}.png'))
                    # UI labels come from escaped user-configurable layout titles.
                    layout=api.get('/api/state').json()['layout'];layout['widgets'][0]['title']='<img src=x onerror=window.BAD_TIMER=1>'
                    assert api.put('/api/layout',json=layout).status_code==200
                    expect(left.locator('h2')).to_contain_text('<img')
                    check('Widget titles are escaped',not page.evaluate('Boolean(window.BAD_TIMER)'))
                    # Legacy overflow remains reachable inline and is not mirrored as current.
                    layout['widgets']=[layout['widgets'][0]];layout['version']+=1
                    assert api.put('/api/layout',json=layout).status_code==200
                    expect(left.locator('.timer-recovery')).to_be_visible();left.locator('summary').click()
                    expect(left.locator('.timer-recovery-row')).to_be_visible()
                    check('Removed-widget running timer remains accessible inline',left.locator('.timer-recovery-row').count()==1 and home_visible())
                    left.locator('[data-timer-legacy]').click()
                    expect(left.locator('.timer-recovery')).to_have_count(0)
                    check('Recovering removed widget cannot stop remaining card',app.state.timers.get(latest['id'])['state']=='running' and app.state.timers.get(b['id'])['state']=='stopped')
                    check('No uncaught JS exceptions',not report['errors'])
                    browser.close()
                    if bridged:display.close()
        finally:
            server.should_exit=True;thread.join(timeout=10)
            report['passed']=sum(i['passed'] for i in report['checks'])
            (out/'timers-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('TOTAL',report['passed'])
if __name__=='__main__':main()
