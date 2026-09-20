"""Full-list UI regression on rebuilt demos; not a physical iPad or V35 test."""
from pathlib import Path
import json,os,shutil
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'artifacts/test-results';OUT.mkdir(parents=True,exist_ok=True)
checks=[];errors=[]
def check(name,condition=True,detail=None):
 checks.append({'name':name,'passed':bool(condition),'detail':detail})
 if not condition:raise AssertionError(f'{name}: {detail}')
FIXTURE="""(n)=>{
 const seed=window.ROOM_DEMO.tasks[0];return Array.from({length:n},(_,i)=>({...seed,
 id:'bulk-'+String(i).padStart(5,'0'),title:'시험 작업 '+i,
 date:Room.addDays('2026-01-01',Math.floor(i/3)),time:['09:00','18:00',null][i%3],
 completed:i%4===0,version:1,series_id:i%3===0?'test-series':null,notes:''})).reverse();
}"""
def main():
 try:
  with sync_playwright() as p:
   b=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox','--disable-dev-shm-usage'])
   page=b.new_page(viewport={'width':1194,'height':834},has_touch=True)
   page.on('pageerror',lambda e:errors.append(str(e)))
   page.set_content((ROOT/'previews/client_preview.html').read_text(),wait_until='domcontentloaded');page.wait_for_selector('.todo-week-strip')
   check('Existing four-widget layout unchanged',page.locator('#grid .widget').count()==4)
   page.evaluate("Object.assign(RoomDisplay.getState().layout.widgets.find(w=>w.id==='tasks'),{type:'all-todos',title:'전체 할 일'});RoomDisplay.home()")
   page.wait_for_selector('#grid .all-todos-list')
   check('All filter is default',page.locator('#grid [data-all-filter=all]').get_attribute('aria-pressed')=='true')
   check('Past, today, future and completed retained',page.locator('#grid .all-task-row').count()==22 and page.locator('#grid .all-task-row.done').count()==9)
   expected=page.evaluate('Room.chronologicalTasks(RoomDisplay.getState().tasks).map(t=>t.id)')
   ids=lambda area='#grid':page.locator(area+' .all-task-row').evaluate_all('(es)=>es.map(e=>e.dataset.taskId)')
   check('Widget date/time order matches contract',ids()==expected)
   first=expected[0];page.locator(f'#grid [data-complete-task="{first}"]').tap();page.wait_for_timeout(120)
   check('Undo completion does not reorder',ids()==expected and not page.evaluate('(id)=>RoomDisplay.getState().tasks.find(t=>t.id===id).completed',first))
   page.locator(f'#grid [data-complete-task="{first}"]').tap();page.wait_for_timeout(120)
   check('Complete persists in current data',page.evaluate('(id)=>RoomDisplay.getState().tasks.find(t=>t.id===id).completed',first))
   page.locator('#grid [data-caldate="2026-09-19"]').tap();page.wait_for_timeout(120)
   check('Calendar selection does not filter full list',ids()==expected)
   page.locator('#grid [data-all-filter=open]').tap();page.wait_for_timeout(100)
   check('Optional open filter',page.locator('#grid .all-task-row').count()==13 and page.locator('#grid .all-task-row.done').count()==0)
   page.locator('#grid [data-all-filter=done]').tap();page.wait_for_timeout(100)
   check('Optional done filter',page.locator('#grid .all-task-row').count()==9)
   page.locator('#grid [data-all-filter=all]').tap();page.wait_for_timeout(100)
   check('Returning to all restores every row',page.locator('#grid .all-task-row').count()==22)
   page.locator('#grid [data-all-today]').tap();page.wait_for_timeout(120)
   check('Today jump scrolls to today or later',page.locator('#grid .all-todos-list').evaluate('(e)=>e.scrollTop')>0)
   page.locator('#grid [data-all-first]').tap();page.wait_for_timeout(80)
   check('First returns to earliest task',page.locator('#grid .all-todos-list').evaluate('(e)=>e.scrollTop')==0)
   for w,h in [(1194,834),(1194,720),(834,1194)]:
    page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(240)
    check(f'No viewport overflow {w}x{h}',page.evaluate('document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight'))
    check(f'Toolbar fits {w}x{h}',page.locator('#grid .all-toolbar').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'))
    check(f'44px completion target {w}x{h}',page.locator('#grid .all-complete').first.evaluate('(e)=>e.offsetWidth>=44&&e.offsetHeight>=44'))
   page.set_viewport_size({'width':1194,'height':834});page.wait_for_timeout(240)
   page.locator('#grid [data-all-expand]').tap();page.wait_for_selector('#focus .all-todos-list')
   check('Expand available',page.locator('#focus').is_visible())
   page.locator('#backButton').tap();page.wait_for_timeout(80)
   data=page.evaluate(FIXTURE,640)
   page.evaluate('(items)=>{RoomDisplay.getState().tasks=items;RoomDisplay.home()}',data);page.wait_for_timeout(120)
   check('640 records render a first batch not a hard cutoff',page.locator('#grid .all-task-row').count()==80)
   page.locator('#grid .all-todos-list').evaluate('(e)=>e.scrollTop=e.scrollHeight');page.wait_for_timeout(180)
   check('Scrolling automatically appends more',page.locator('#grid .all-task-row').count()>80)
   for _ in range(10):
    more=page.locator('#grid [data-all-more]')
    if not more.count():break
    more.evaluate('(e)=>e.click()');page.wait_for_timeout(40)
   check('Every record reachable beyond 300',page.locator('#grid .all-task-row').count()==640)
   check('No duplicate or missing task IDs',ids()==[f'bulk-{i:05d}' for i in range(640)])
   # Test button once at a deep scroll position; explicit state, no optimistic mutation.
   target='bulk-00350';page.locator(f'#grid [data-complete-task="{target}"]').scroll_into_view_if_needed();page.wait_for_timeout(70)
   top=page.locator('#grid .all-todos-list').evaluate('(e)=>e.scrollTop')
   page.locator(f'#grid [data-complete-task="{target}"]').tap();page.wait_for_timeout(180)
   check('Deep completion retains order and loaded rows',ids()==[f'bulk-{i:05d}' for i in range(640)])
   check('Deep completion retains scroll',abs(page.locator('#grid .all-todos-list').evaluate('(e)=>e.scrollTop')-top)<3)
   page.evaluate("RoomDisplay.selectDate('2027-02-01')");page.wait_for_timeout(130)
   check('Other date selection retains full-list position',abs(page.locator('#grid .all-todos-list').evaluate('(e)=>e.scrollTop')-top)<3)
   # Keyboard-free client input boundary and escaped content.
   page.evaluate("RoomDisplay.getState().tasks[0].title='<img src=x onerror=alert(1)>';RoomDisplay.getState().tasks[0].notes='<svg onload=alert(1)>';RoomDisplay.home()")
   page.wait_for_timeout(100)
   check('Task title escaped',page.locator('.all-task-title img').count()==0)
   check('No client text editors',page.locator('input,textarea,select,[contenteditable=true]').count()==0)
   page.evaluate("Object.assign(RoomDisplay.getState().layout.widgets.find(w=>w.id==='tasks'),{w:1,h:1});RoomDisplay.home()")
   page.wait_for_timeout(120)
   check('1x1 uses compact summary',page.locator('#grid [data-widget-id=tasks] .compact').count()==1)
   page.locator('#grid [data-widget-id=tasks]').tap();page.wait_for_timeout(100)
   check('Compact tile expands into full list',page.locator('#focus .all-task-row').count()>0)
   page.close()
   page=b.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
   page.set_content((ROOT/'previews/manager_preview.html').read_text(),wait_until='domcontentloaded');page.wait_for_selector('.kpis')
   page.evaluate("RoomManager.navigate('tasks')");page.wait_for_selector('#taskRows')
   check('Manager defaults to all dates',page.locator('[name=showAll]').input_value()=='all')
   check('Manager defaults to all statuses',page.locator('[name=taskStatus]').input_value()=='all')
   check('Manager shows full sample history',page.locator('#taskRows .manager-task').count()==22)
   check('Date input disabled in all-date mode',page.locator('[name=filterDate]').is_disabled())
   page.locator('[name=showAll]').select_option('day');page.wait_for_timeout(60)
   check('Date-only mode remains available',page.locator('#taskRows .manager-task').count()==4)
   page.locator('[name=showAll]').select_option('all');page.wait_for_timeout(60)
   page.locator('[name=taskStatus]').select_option('open');page.wait_for_timeout(60)
   check('Manager optional status filter',page.locator('#taskRows .manager-task').count()==13)
   page.locator('[name=taskStatus]').select_option('all');page.wait_for_timeout(60)
   # Fixture updates target existing demo state through a bridged parent state getter.
   data=page.evaluate(FIXTURE,640)
   page.evaluate('(items)=>{RoomManager.getState().tasks=items;RoomManager.navigate("tasks")}',data);page.wait_for_timeout(70)
   check('Manager initial batch of 100',page.locator('#taskRows .manager-task').count()==100)
   for _ in range(8):
    btn=page.locator('[data-op=tasks-more]')
    if not btn.count():break
    btn.evaluate('(e)=>e.click()');page.wait_for_timeout(35)
   check('Manager reaches last record beyond 300',page.locator('#taskRows .manager-task').count()==640)
   check('Manager is chronological and complete',page.locator('#taskRows .manager-task').evaluate_all('(es)=>es.map(e=>e.dataset.taskId)')==[f'bulk-{i:05d}' for i in range(640)])
   page.locator('[name=filterQuery]').fill('시험 작업 639');page.locator('[name=filterQuery]').dispatch_event('change');page.wait_for_timeout(70)
   check('Search covers data beyond first batch',page.locator('#taskRows .manager-task').count()==1 and '639' in page.locator('#taskRows').inner_text())
   page.evaluate("RoomManager.navigate('layout')");page.wait_for_selector('#widgetForm')
   page.locator('[data-wid=tasks]').click();page.wait_for_timeout(70)
   page.locator('[name=widgetType]').select_option('all-todos');page.wait_for_timeout(70)
   page.locator('[data-op=save-layout]').click();page.wait_for_timeout(130)
   check('Opt-in same-position widget type switch',page.evaluate("RoomManager.getState().layout.widgets.find(w=>w.id==='tasks').type==='all-todos'"))
   page.evaluate("RoomManager.navigate('overview')");page.wait_for_selector('#previewFrame')
   frame=page.locator('#previewFrame').element_handle().content_frame();frame.wait_for_selector('.all-todos-list')
   check('New widget appears in manager embedded preview',frame.locator('#grid .all-todos-list').count()==1)
   before=page.locator('#previewBox').bounding_box()['width'];page.wait_for_timeout(1200)
   check('Preview width remains stable',abs(page.locator('#previewBox').bounding_box()['width']-before)<.1)
   # Shared utility high-volume sort on synthetic 10k tasks, not browser memory benchmark.
   data=page.evaluate(FIXTURE,10000)
   result=page.evaluate('(items)=>{const copy=items.slice();const sorted=Room.chronologicalTasks(items);return {count:sorted.length,first:sorted[0].id,last:sorted.at(-1).id,unchanged:copy.every((t,i)=>t===items[i])}}',data)
   check('Shared ordering covers 10000 without mutating input',result=={'count':10000,'first':'bulk-00000','last':'bulk-09999','unchanged':True})
   check('No uncaught JS errors',not errors,errors)
   b.close()
 finally:
  result={'scope':'Chromium standalone production-source demos; synthetic data; not physical V35/Safari','checks':len(checks),'passed':sum(t['passed'] for t in checks),'errors':errors,'results':checks}
  (OUT/'all-tasks-ui-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:result[k] for k in ['checks','passed','errors']}))
if __name__=='__main__':main()
