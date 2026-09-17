"""Chromium UI checks against standalone demos. Not a physical Safari/LAN E2E test."""
import json,os,shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
REPORTS=ROOT/'artifacts'/'test-results';REPORTS.mkdir(parents=True,exist_ok=True)
SHOTS=ROOT/'artifacts'/'screenshots';SHOTS.mkdir(parents=True,exist_ok=True);OUT=ROOT/'previews';results=[]

def check(name,condition=True):
 if not condition:raise AssertionError(name)
 results.append({'name':name,'passed':True})

def run():
 with sync_playwright() as p:
  executable=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome')
  browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1194,'height':834},device_scale_factor=1,has_touch=True)
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.set_content((OUT/'client_preview.html').read_text(encoding='utf-8'),wait_until='domcontentloaded');page.wait_for_selector('#grid .widget');page.wait_for_timeout(250)
  check('Default four widgets on 8x6 grid',page.locator('#grid .widget').count()==4)
  check('Client has no editing input, textarea, select, contenteditable',page.locator('input,textarea,select,[contenteditable=true]').count()==0)
  check('Clock follows demo/server reference',page.locator('[data-clock-main]').first.inner_text()=='09:41')
  page.screenshot(path=str(SHOTS/'client_landscape.png'))
  page.locator('[data-widget-id=clock]').tap();page.wait_for_selector('#focus:not(.hidden)');check('Touch expands clock',page.locator('#focus [data-clock-main]').count()==1)
  page.locator('#backButton').tap();page.wait_for_selector('#focus',state='hidden');check('Back returns to dashboard',page.locator('#display').is_visible())
  page.locator('#grid [data-caldate="2026-09-18"]').tap();page.wait_for_timeout(100);check('Calendar date selects corresponding tasks','9월 18일' in page.locator('#grid [data-widget-id=tasks]').inner_text())
  page.locator('#grid [data-widget-id=calendar] .calendar-toolbar h2').tap();page.wait_for_selector('#focus:not(.hidden)');page.screenshot(path=str(SHOTS/'calendar_expanded.png'))
  page.locator('#focus [data-caldate="2026-09-19"]').tap();page.wait_for_timeout(100);check('Expanded calendar date opens expanded tasks',page.locator('#focus [data-widget-id=tasks]').count()==1)
  page.locator('#backButton').tap()
  for width,height,name in [(1194,834,'client_landscape'),(1194,720,'client_safari_height'),(834,1194,'client_portrait')]:
   page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(250)
   overflow=page.evaluate('document.documentElement.scrollWidth>innerWidth || document.documentElement.scrollHeight>innerHeight')
   check(f'No page overflow at {width}x{height}',not overflow)
   if name!='client_landscape':page.screenshot(path=str(SHOTS/(name+'.png')))
  page.evaluate("RoomDisplay.getState().tasks.push({id:'xss',title:'<img src=x onerror=alert(1)>',date:'2026-09-17',category:'personal',completed:false});RoomDisplay.selectDate('2026-09-17')")
  page.wait_for_timeout(100);check('Untrusted task text remains text, not HTML',page.locator('.task-title img').count()==0 and '<img' in page.locator('[data-widget-id=tasks]').inner_text())
  page.close()
  page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1);page.on('pageerror',lambda e:errors.append(str(e)))
  page.set_content((OUT/'manager_preview.html').read_text(encoding='utf-8'),wait_until='domcontentloaded');page.wait_for_selector('.kpis');page.wait_for_timeout(250)
  check('Manager dashboard and live-style preview load',page.locator('.kpi').count()==4)
  page.screenshot(path=str(SHOTS/'manager_overview.png'))
  page.evaluate("RoomManager.navigate('tasks')");page.wait_for_selector('#taskForm');page.screenshot(path=str(SHOTS/'manager_tasks.png'))
  page.locator('#taskForm [name=title]').fill('테스트 반복');page.locator('#taskForm [name=date]').fill('2026-09-17');page.locator('#taskForm [name=frequency]').select_option('daily');page.locator('#taskForm [name=until]').fill('2026-09-19');page.locator('[data-op=preview-repeat]').click();page.wait_for_timeout(100)
  check('Recurrence preview includes final date', '총 3개' in page.locator('#repeatPreview').inner_text())
  before=page.evaluate('RoomManager.getState().tasks.length');page.locator('#taskForm button[type=submit]').click();page.wait_for_timeout(150)
  check('Manager creates independent recurring task occurrences',page.evaluate('RoomManager.getState().tasks.length')==before+3)
  completed=page.evaluate('RoomManager.getState().tasks.filter(t=>t.completed).length');page.locator('[data-op=complete]:not(.done)').first.click();page.wait_for_timeout(150)
  check('Completion is handled in manager',page.evaluate('RoomManager.getState().tasks.filter(t=>t.completed).length')==completed+1)
  page.evaluate("RoomManager.navigate('layout')");page.wait_for_selector('#layoutBoard');page.screenshot(path=str(SHOTS/'manager_layout.png'))
  page.locator('[name=gridRows]').fill('8');page.locator('[name=gridRows]').dispatch_event('change');page.wait_for_timeout(50);page.locator('[data-op=add-widget][data-id=note]').click();page.wait_for_timeout(50)
  check('Folder widget can be added to configurable grid',page.locator('.layout-tile').count()==5)
  page.locator('#widgetConfig').fill('{"text":"새 위젯 테스트","caption":"테스트"}');page.locator('[data-op=save-layout]').click();page.wait_for_timeout(150)
  check('Layout and widget config apply',page.evaluate('RoomManager.getState().layout.widgets.some(w=>w.type==="note"&&w.config.text==="새 위젯 테스트")'))
  for tab,selector in [('devices','#pairForm'),('voice','#voiceTextForm'),('settings','#settingsForm')]:
   page.evaluate(f"RoomManager.navigate('{tab}')");page.wait_for_selector(selector);check(f'Manager {tab} GUI loads');page.screenshot(path=str(SHOTS/f'manager_{tab}.png'))
  page.evaluate("RoomManager.navigate('voice')");page.locator('#voiceTextForm [name=text]').fill('새로운 음성 입력');page.locator('#voiceTextForm button[type=submit]').click();page.wait_for_timeout(100)
  check('Text intake remains in review inbox','새로운 음성 입력' in page.locator('.voice-item').first.inner_text())
  check('No uncaught JavaScript errors',not errors)
  browser.close()
 (REPORTS/'browser-test-results.json').write_text(json.dumps({'checks':len(results),'passed':len(results),'engine':'Chromium via Playwright; standalone HTML set_content, not actual iPad/Safari or live browser network E2E','results':results},ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'passed':len(results),'errors':errors},ensure_ascii=False))
if __name__=='__main__':run()
