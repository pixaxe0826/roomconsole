"""Real loopback UI/API test with synthetic data/time; not physical iPad audio."""
from __future__ import annotations
import json,os,shutil,socket,sys,tempfile,threading,time
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx,uvicorn
from playwright.sync_api import sync_playwright,expect
from app.main import create_app

def main():
 out=ROOT/'artifacts/test-results';out.mkdir(parents=True,exist_ok=True)
 shots=ROOT/'artifacts/screenshots';shots.mkdir(parents=True,exist_ok=True)
 bridge_mode=os.getenv('HUB_BROWSER_BRIDGE')=='1'
 report={'checks':[],'browser_errors':[],'environment':'Chromium loopback / synthetic SQLite + injected clock; physical audio NOT_RUN'}
 if bridge_mode:report['environment']='Exact UI + Python HTTP/SQLite bridge / injected invalidations; direct browser network and physical audio NOT_RUN'
 def check(name,ok):
  report['checks'].append({'name':name,'passed':bool(ok)});print(('PASS ' if ok else 'FAIL ')+name,flush=True);assert ok,name
 with tempfile.TemporaryDirectory(prefix='room-life-browser-') as tmp:
  app=create_app(Path(tmp),weather_enabled=False);at=[datetime.fromisoformat('2026-09-20T10:00:00+09:00').timestamp()];app.state.life.clock=lambda:at[0]
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  base=f'http://127.0.0.1:{port}';server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'));thread=threading.Thread(target=server.run,daemon=True);thread.start()
  try:
   for _ in range(200):
    if server.started:break
    time.sleep(.025)
   assert server.started
   with httpx.Client(base_url=base,headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
    layout=api.get('/api/state').json()['layout'];layout['widgets'][2].update(type='note',title='메모');layout['widgets'][3].update(type='alarms',title='알람');assert api.put('/api/layout',json=layout).status_code==200
    pair=api.post('/api/devices/pair',json={'name':'browser-fixture'}).json()
    with sync_playwright() as p:
     executable=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or shutil.which('chromium');options={'headless':True,'args':['--no-sandbox']}
     if executable:options['executable_path']=executable
     browser=p.chromium.launch(**options);manager=browser.new_page(viewport={'width':1440,'height':1000});tablet=browser.new_page(viewport={'width':1194,'height':834},has_touch=True)
     for page in [manager,tablet]:page.on('pageerror',lambda err:report['browser_errors'].append(str(err)))
     if bridge_mode:
      from assistant_browser import load_bridged
      display_api=httpx.Client(base_url=base,headers={'X-Room-Request':'1'})
      display_api.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]})
      load_bridged(manager,api,'manager')
     else:
      manager.goto(base+'/manager');manager.locator('#loginForm input').fill(app.state.admin_token);manager.locator('#loginForm button').click()
     manager.locator('#managerApp:not(.hidden)').wait_for()
     manager.evaluate("RoomManager.navigate('notes')");manager.locator('#lifeWorkspace input[name=title]').fill('함께 보는 작은 메모');manager.locator('#lifeWorkspace textarea[name=body]').fill('<script>window.PWNED=true</script>\n줄바꿈을 보존합니다.');manager.locator('#lifeWorkspace input[name=shared]').check();manager.locator('#lifeWorkspace input[name=pinned]').check();manager.locator('#lifeWorkspace button[type=submit]').click();manager.locator('.life-list-item').filter(has_text='함께 보는 작은 메모').wait_for()
     check('Manager note saves to SQLite with explicit sharing',api.get('/api/life/notes').json()['items'][0]['shared'])
     if bridge_mode:load_bridged(tablet,display_api,'client')
     else:tablet.goto(base+pair['path'])
     tablet.locator('.life-notes').wait_for();expect(tablet.locator('.life-notes')).to_contain_text('함께 보는 작은 메모')
     check('Paired display renders shared note and escaped text',not tablet.evaluate('Boolean(window.PWNED)') and '<script>' in tablet.locator('.life-notes').inner_text())
     check('No notes editor on display',tablet.locator('#grid input,#grid textarea').count()==0)
     manager.locator('[data-life-op=new]').click();manager.locator('#lifeWorkspace input[name=title]').fill('비공개 메모');manager.locator('#lifeWorkspace button[type=submit]').click();manager.locator('.life-list-item').filter(has_text='비공개 메모').wait_for()
     check('Private note excluded from display DTO',len(api.get('/api/life/display').json()['notes']['items'])==1)
     manager.locator('.life-list-item').filter(has_text='함께 보는 작은 메모').click();manager.locator('#lifeWorkspace textarea').fill('자동 갱신 중에도 보존할 초안');manager.evaluate('RoomManager.refresh()');manager.wait_for_timeout(400)
     check('Manager refresh preserves unsaved draft',manager.locator('#lifeWorkspace textarea').input_value()=='자동 갱신 중에도 보존할 초안');manager.screenshot(path=str(shots/'notes-manager.png'),full_page=True)
     manager.evaluate("RoomManager.navigate('alarms')");manager.locator('#lifeWorkspace input[name=label]').fill('몸을 움직일 시간');manager.locator('#lifeWorkspace input[name=time]').fill('10:01');manager.locator('#lifeWorkspace input[name=date]').fill('2026-09-20');manager.locator('#lifeWorkspace button[type=submit]').click();manager.locator('.life-list-item').filter(has_text='몸을 움직일 시간').wait_for()
     check('Manager schedules a real one-shot alarm',len(api.get('/api/life/alarms').json()['items'])==1)
     tablet.locator('[data-life-sound-state]').filter(has_text='소리 사용 안 함').wait_for();check('Sound not enabled automatically',tablet.evaluate('RoomLife.soundStatus()').startswith('소리 사용 안 함'))
     tablet.evaluate("""() => { window.immediateStops=0;const proto=(window.AudioContext||window.webkitAudioContext).prototype, original=proto.createOscillator;proto.createOscillator=function(){const o=original.call(this),stop=o.stop.bind(o);o.stop=(at)=>{if(at===undefined)window.immediateStops++;return stop(at)};return o}; }""")
     tablet.locator('[data-life-alarm=enable]').first.click();expect(tablet.locator('[data-life-sound-state]').first).to_contain_text('소리 사용 중');check('Gesture resumes WebAudio (physical sound not verified)',True)
     check('Manual test tone not cancelled by idle repaint',tablet.evaluate('window.immediateStops===0'))
     at[0]+=60;tablet.locator('#lifeAlarmAlert').wait_for(timeout=12000);expect(tablet.locator('#lifeAlarmAlert')).to_contain_text('몸을 움직일 시간');check('Timer reaches paired browser (transport stated in report)',app.state.life.events()[0]['state']=='ringing');tablet.screenshot(path=str(shots/'alarm-ringing-client.png'))
     tablet.locator('#lifeAlarmAlert [data-life-alarm=snooze]').click();tablet.locator('#lifeAlarmAlert').wait_for(state='detached');check('Five minute snooze persisted',app.state.life.events()[0]['state']=='snoozed')
     at[0]+=300;tablet.locator('#lifeAlarmAlert').wait_for(timeout=12000);tablet.locator('#lifeAlarmAlert [data-life-alarm=ack]').click();tablet.locator('#lifeAlarmAlert').wait_for(state='detached');check('Acknowledgement ends existing occurrence',app.state.life.events()[0]['state']=='acknowledged')
     if bridge_mode:
      tablet.goto('about:blank');load_bridged(tablet,display_api,'client')
     else:tablet.reload()
     tablet.locator('.life-alarms').wait_for();check('Reload requires renewed sound permission',tablet.evaluate('RoomLife.soundStatus()').startswith('소리 사용 안 함'));manager.screenshot(path=str(shots/'alarms-manager.png'),full_page=True);tablet.screenshot(path=str(shots/'notes-alarms-client.png'))
     api.post('/api/life/alarms',json={'request_id':'browser-alarm-second','label':'재연결 테스트','date':'2026-09-20','time':'10:07','timezone':'Asia/Seoul'});at[0]+=60;tablet.locator('#lifeAlarmAlert').wait_for(timeout=12000);tablet.evaluate('RoomLife.offline()');check('Offline suppresses active alert immediately',tablet.locator('#lifeAlarmAlert').count()==0);tablet.evaluate('RoomLife.stopDisplay()');check('Revocation clears sound state',tablet.evaluate('RoomLife.soundStatus()')=='연결 권한 없음')
     compact_layout=api.get('/api/state').json()['layout'];compact_layout['widgets'][2]['h']=2;compact_layout['widgets'][3]['h']=2;api.put('/api/layout',json=compact_layout)
     tablet.locator('.life-alarms.compact').wait_for()
     check('Default 4x2 alarm controls and next time fit',tablet.locator('.life-alarms').evaluate("e=>e.querySelector('.life-next').getBoundingClientRect().bottom<=e.getBoundingClientRect().bottom"))
     check('Default 4x2 note navigation fits',tablet.locator('.life-notes').evaluate("e=>e.querySelector('.life-note-nav').getBoundingClientRect().bottom<=e.getBoundingClientRect().bottom"))
     for width in [834,390]:
      manager.set_viewport_size({'width':width,'height':1000});manager.wait_for_timeout(200);check('Manager no overflow '+str(width),manager.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
      tablet.set_viewport_size({'width':width,'height':1000});tablet.wait_for_timeout(300);check('Display no overflow '+str(width),tablet.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
     check('No uncaught browser exceptions',not report['browser_errors']);browser.close()
     if bridge_mode:display_api.close()
  finally:
   server.should_exit=True;thread.join(timeout=10);report['passed']=sum(c['passed'] for c in report['checks']);(out/'life-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 print('TOTAL',report['passed'])
if __name__=='__main__':main()
