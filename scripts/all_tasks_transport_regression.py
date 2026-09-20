"""New widget + production completion logic, intentionally mocked HTTP/WS."""
import json,os,shutil
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from todo_transport_regression import fixture_html
ROOT=Path(__file__).resolve().parents[1]
def run():
 checks=[];errors=[]
 def check(name,ok=True):
  checks.append({'name':name,'passed':bool(ok)})
  if not ok:raise AssertionError(name)
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1194,'height':834},has_touch=True);page.on('pageerror',lambda e:errors.append(str(e)))
  def reset():
   html=fixture_html().replace("TEST.snapshot=", "Object.assign(TEST.server.layout.widgets.find(w=>w.id==='tasks'),{type:'all-todos',title:'전체 할 일'});\nTEST.snapshot=",1)
   page.goto('about:blank');page.set_content(html,wait_until='domcontentloaded');page.wait_for_selector('.all-todos-list');expect(page.locator('#grid [data-complete-task=task-1]')).to_be_enabled()
  reset();sel='#grid [data-complete-task=task-1]';before=page.locator('#grid .all-task-row').evaluate_all('(es)=>es.map(e=>e.dataset.taskId)')
  page.evaluate('TEST.hold=true');page.locator(sel).tap();expect(page.locator(sel)).to_be_disabled();page.locator(sel).dispatch_event('click')
  check('Full-list pending write is a single request',page.evaluate('TEST.requests.length')==1)
  check('Full-list is not optimistically completed',page.locator(sel).get_attribute('aria-checked')=='false')
  page.evaluate('TEST.hold=false;TEST.pending.shift()()');expect(page.locator(sel)).to_have_attribute('aria-checked','true')
  check('Full-list successful save retains chronology',page.locator('#grid .all-task-row').evaluate_all('(es)=>es.map(e=>e.dataset.taskId)')==before)
  page.locator(sel).tap();expect(page.locator(sel)).to_have_attribute('aria-checked','false');check('Full-list undo sends explicit false',page.evaluate('TEST.requests[1].body.completed===false'))
  page.evaluate('TEST.networkFailure=true;TEST.getFailure=true');page.locator(sel).tap();expect(page.locator(sel)).to_be_disabled()
  check('Offline shows saved state without false completion',page.locator(sel).get_attribute('aria-checked')=='false')
  count=page.evaluate('TEST.requests.length');page.locator(sel).dispatch_event('click');check('No queued offline writes',page.evaluate('TEST.requests.length')==count)
  page.locator('#grid [data-all-filter=done]').tap();page.wait_for_timeout(80);check('Read-only filtering works offline',page.locator('#grid .all-task-row').count()==9)
  page.locator('#grid [data-all-filter=all]').tap();page.wait_for_timeout(80)
  page.evaluate('TEST.networkFailure=false;TEST.getFailure=false;TEST.message({type:"hello"})');expect(page.locator(sel)).to_be_enabled()
  check('Reconnect does not replay writes',page.evaluate('TEST.requests.length')==count)
  reset();page.evaluate('TEST.hold=true');page.locator(sel).tap();page.evaluate("const t=TEST.server.tasks.find(t=>t.id==='task-1');t.title='다른 화면에서 수정';t.version++;TEST.server.revision++;TEST.pending.shift()()")
  expect(page.locator('.toast')).to_contain_text('다른 화면에서 변경');check('Version conflict retains authoritative data',page.evaluate("RoomDisplay.getState().tasks.find(t=>t.id==='task-1').title==='다른 화면에서 수정'"))
  reset();page.evaluate('TEST.unauthorized=true');page.locator(sel).tap();page.wait_for_selector('.loading-card');check('Expired pairing removes full-list action buttons',page.locator('[data-complete-task]').count()==0)
  check('No uncaught JS errors',not errors)
  browser.close()
 output=ROOT/'artifacts/test-results/all-tasks-transport-results.json';output.parent.mkdir(parents=True,exist_ok=True)
 result={'scope':'Mocked HTTP/WS/module loading; actual all-todos widget and client completion handler','checks':len(checks),'passed':sum(c['passed'] for c in checks),'errors':errors,'results':checks};output.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:result[k] for k in ['checks','passed','errors']}))
if __name__=='__main__':run()
