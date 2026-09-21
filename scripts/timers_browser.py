"""Actual UI + SQLite/Uvicorn countdowns; synthetic clock, not physical audio."""
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
    report={'checks':[],'errors':[],'scope':('Python HTTP bridge / injected WS; ' if bridged else 'Chromium direct HTTP/WS; ')+'actual UI + SQLite, synthetic clock; physical sound NOT_RUN'}
    def check(name,result):
        report['checks'].append({'name':name,'passed':bool(result)});print(('PASS ' if result else 'FAIL ')+name,flush=True);assert result,name
    with tempfile.TemporaryDirectory() as tmp:
        app=create_app(tmp,weather_enabled=False);at=[time.time()];app.state.timers.clock=lambda:at[0]
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}';server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            assert server.started
            with httpx.Client(base_url=base,trust_env=False,headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
                layout=api.get('/api/state').json()['layout'];layout['widgets']=[{'id':f'timer-{i}','type':'timers','title':'타이머 '+str(i+1),'x':i*2,'y':0,'w':2,'h':2,'config':{}} for i in range(2)]
                assert api.put('/api/layout',json=layout).status_code==200
                pair=api.post('/api/devices/pair',json={'name':'timer-browser-test'}).json()
                with sync_playwright() as p:
                    executable=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or shutil.which('chromium')
                    opts={'headless':True,'args':['--no-sandbox']}
                    if executable:opts['executable_path']=executable
                    browser=p.chromium.launch(**opts);page=browser.new_page(viewport={'width':1440,'height':1000},has_touch=True)
                    page.on('pageerror',lambda e:report['errors'].append(str(e)))
                    if bridged:
                        from assistant_browser import load_bridged
                        display=httpx.Client(base_url=base,trust_env=False,headers={'X-Room-Request':'1'})
                        assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
                        load_bridged(page,display,'client')
                    else:page.goto(base+pair['path'])
                    page.locator('#grid .rh-timers').first.wait_for()
                    left=page.locator('#grid [data-widget-id="timer-0"]');right=page.locator('#grid [data-widget-id="timer-1"]')
                    check('Two timer widgets rendered with no added default layout',page.locator('#grid .rh-timers').count()==2)
                    inp=left.locator('input');check('Exact one-second range',inp.get_attribute('min')=='1' and inp.get_attribute('max')=='600' and inp.get_attribute('step')=='1')
                    inp.fill('240');left.locator('button[type=submit]').click()
                    expect(left.locator('.eyebrow')).to_have_text('1개 실행',timeout=10000)
                    check('Click starts a real 240-second timer',api.get('/api/timers').json()['items'][0]['duration_seconds']==240)
                    right.locator('input').fill('600');right.locator('button[type=submit]').click()
                    expect(left.locator('.eyebrow')).to_have_text('2개 실행',timeout=10000)
                    check('One shared concurrent list across widget instances',api.get('/api/timers').json()['active_count']==2 and right.locator('.eyebrow').inner_text()=='2개 실행')
                    check('Compact controls fit 2x2',left.locator('.rh-timers').evaluate('e=>e.scrollHeight<=e.clientHeight+1 && e.scrollWidth<=e.clientWidth+1'))
                    check('Countdown and stop controls visible in 2x2',left.locator('.timer-items').evaluate('e=>{const a=e.getBoundingClientRect(),b=e.querySelector(".timer-row").getBoundingClientRect();return a.height>=b.height && b.top>=a.top-1 && b.bottom<=a.bottom+1;}'))
                    page.screenshot(path=str(shots/'timers-2x2.png'))
                    left.locator('[data-timer-expand]').click();page.locator('#focus:not(.hidden)').wait_for()
                    check('Expanded view exposes every timer',page.locator('#focus .timer-row').count()==2)
                    page.screenshot(path=str(shots/'timers-expanded.png'))
                    initial=api.get('/api/timers').json();current=initial['current_id'];other=next(i['id'] for i in initial['items'] if i['id']!=current)
                    page.locator(f'#focus [data-timer-stop="{current}"]').click()
                    expect(page.locator('#focus .eyebrow')).to_have_text('1개 실행')
                    check('Stop targets one timer only',app.state.timers.get(current)['state']=='stopped' and app.state.timers.get(other)['state']=='running')
                    page.locator('#focus input').fill('1');page.locator('#focus button[type=submit]').click()
                    expect(page.locator('#focus .eyebrow')).to_have_text('2개 실행')
                    identity=api.get('/api/timers').json()['current_id']
                    at[0]+=2
                    expect(page.locator(f'#focus [data-timer-id="{identity}"] [data-timer-status]')).to_have_text('시간 종료',timeout=10000)
                    check('One-second expiry uses server state',app.state.timers.get(identity)['state']=='expired')
                    check('Sound is opt-in, not automatically enabled',page.evaluate('RoomTimers.soundStatus()')=='소리 꺼짐')
                    awaitable=page.evaluate('RoomTimers.enableSound().then(()=>RoomTimers.soundStatus())')
                    check('WebAudio enable attempted without claiming physical sound',awaitable in {'소리 허용됨','소리 꺼짐'})
                    page.evaluate('RoomTimers.mute()');check('Mute resets authorization',page.evaluate('RoomTimers.soundStatus()')=='소리 꺼짐')
                    page.locator('#focus input').fill('601');count=len(api.get('/api/timers').json()['items']);page.locator('#focus button[type=submit]').click();page.wait_for_timeout(100)
                    check('Browser cannot start 601 seconds',len(api.get('/api/timers').json()['items'])==count)
                    for width in (834,390):
                        page.set_viewport_size({'width':width,'height':1000});page.wait_for_timeout(120)
                        check('Expanded no horizontal overflow '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
                    # No unescaped label data reaches the DOM.
                    api.post('/api/timers/start',json={'request_id':'xss-test','duration_seconds':300,'label':'<img src=x onerror=window.BAD_TIMER=1>'})
                    expect(page.locator('#focus .timer-info>strong').filter(has_text='<img')).to_have_count(1)
                    check('Timer labels are escaped',not page.evaluate('Boolean(window.BAD_TIMER)'))
                    check('No uncaught JS exceptions',not report['errors'])
                    browser.close()
                    if bridged:display.close()
        finally:
            server.should_exit=True;thread.join(timeout=10)
            report['passed']=sum(i['passed'] for i in report['checks'])
            (out/'timers-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('TOTAL',report['passed'])
if __name__=='__main__':main()
