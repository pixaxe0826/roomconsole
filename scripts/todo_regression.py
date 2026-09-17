"""Todo UI regression against rebuilt standalone demos (not physical Safari)."""
from pathlib import Path
import json,os,shutil
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
REPORTS=ROOT/'artifacts'/'test-results';REPORTS.mkdir(parents=True,exist_ok=True)
SHOTS=ROOT/'artifacts'/'screenshots';SHOTS.mkdir(parents=True,exist_ok=True)

def run():
 results=[];errors=[]
 def check(name,ok=True,detail=None):
  results.append({'name':name,'passed':bool(ok),'detail':detail})
  if not ok:raise AssertionError(f'{name}: {detail}')
 def dates(page,area='#grid'):
  return page.locator(area+' [data-widget-id=tasks] .day-chip').evaluate_all('(es)=>es.map(e=>e.dataset.date)')
 def labels(page,area='#grid'):
  return page.locator(area+' [data-widget-id=tasks] .day-chip>span').all_text_contents()
 def selected(page,area='#grid'):
  return page.locator(area+' [data-widget-id=tasks] .day-chip.active').get_attribute('data-date')
 try:
  with sync_playwright() as p:
   exe=os.getenv('CHROMIUM_PATH') or shutil.which('chromium')
   browser=p.chromium.launch(executable_path=exe,args=['--no-sandbox','--disable-dev-shm-usage'])
   page=browser.new_page(viewport={'width':1194,'height':834},has_touch=True)
   page.on('pageerror',lambda e:errors.append(str(e)))
   page.set_content((ROOT/'previews/client_preview.html').read_text('utf-8'),wait_until='domcontentloaded')
   page.wait_for_selector('.todo-week-strip')
   check('Fixed Sunday through Saturday labels',labels(page)==['일','월','화','수','목','금','토'])
   check('Initial full week 2026-09-13 to 2026-09-19',dates(page)==[f'2026-09-{n}' for n in range(13,20)])
   for i in range(3):page.locator('#grid [data-week-step="1"]').tap();page.wait_for_timeout(60)
   check('Three next-week presses move beyond seven days',selected(page)=='2026-10-08')
   check('Month calendar follows week navigation',page.locator('#grid .calendar-toolbar h2').inner_text()=='2026년 10월')
   for i in range(3):page.locator('#grid [data-week-step="-1"]').tap();page.wait_for_timeout(60)
   check('Previous weeks restore selected weekday',selected(page)=='2026-09-17')
   page.locator('#grid [data-caldate="2026-09-20"]').tap();page.wait_for_timeout(70)
   check('Calendar Sunday selection starts at Sunday',dates(page)[0]=='2026-09-20' and dates(page)[-1]=='2026-09-26')
   page.locator('#grid [data-date="2026-09-26"]').tap();page.wait_for_timeout(70)
   check('Saturday selection does not rotate weekday order',dates(page)[0]=='2026-09-20' and labels(page)[-1]=='토')
   for day,start,end,label in [('2027-01-01','2026-12-27','2027-01-02','New Year'),('2028-02-29','2028-02-27','2028-03-04','Leap day')]:
    page.evaluate('(d)=>RoomDisplay.selectDate(d)',day);page.wait_for_timeout(70)
    check(label+' uses one Sunday-Saturday week',dates(page)[0]==start and dates(page)[-1]==end)
   page.locator('#grid [data-week-today]').tap();page.wait_for_timeout(70)
   check('Today control returns to today',selected(page)=='2026-09-17')
   check('Week and date controls do not expand the widget',page.locator('#focus').is_hidden())
   before=page.evaluate("RoomDisplay.getState().tasks.find(t=>t.id==='task-1')")
   page.locator('#grid [data-complete-task="task-1"]').tap();page.wait_for_timeout(80)
   after=page.evaluate("RoomDisplay.getState().tasks.find(t=>t.id==='task-1')")
   check('Client touch marks task complete',after['completed'] and after['version']==before['version']+1)
   check('Only completion fields change',all(after[k]==before[k] for k in ['id','title','date','notes','time','category','series_id']))
   check('Completion button does not open widget',page.locator('#focus').is_hidden())
   check('Completion refreshes progress', '2 / 4 완료' in page.locator('#grid .todo-progress').inner_text())
   check('Calendar task marker becomes completed',page.locator('#grid [data-caldate="2026-09-17"] .day-event').count()>0)
   page.locator('#grid [data-complete-task="task-1"]').tap();page.wait_for_timeout(70)
   check('Second touch undoes completion',not page.evaluate("RoomDisplay.getState().tasks.find(t=>t.id==='task-1').completed"))
   page.locator('#grid [data-widget-id=tasks] h2').tap();page.wait_for_selector('#focus:not(.hidden) .todo-week-strip')
   page.locator('#focus [data-week-step="1"]').tap();page.wait_for_timeout(70)
   check('Expanded view has weekly navigation',selected(page,'#focus')=='2026-09-24')
   page.locator('#focus [data-week-today]').tap();page.wait_for_timeout(70)
   page.locator('#focus [data-complete-task="task-2"]').tap();page.wait_for_timeout(70)
   check('Expanded completion affects only one recurring occurrence',page.evaluate("RoomDisplay.getState().tasks.filter(t=>t.series_id==='series-reading'&&t.completed).length")==1)
   check('Expanded view stays open after completing',page.locator('#focus').is_visible())
   page.locator('#focus [data-complete-task="task-2"]').tap();page.wait_for_timeout(70)
   page.screenshot(path=str(SHOTS/'todo_expanded_v011.png'))
   page.locator('#backButton').tap();page.wait_for_timeout(80)
   for width,height in [(1194,834),(1194,720),(834,1194)]:
    page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(220)
    check(f'No page overflow at {width}x{height}',page.evaluate('document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight'))
    check(f'Date row fits at {width}x{height}',page.locator('#grid .todo-week-strip').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'))
    check(f'Completion touch target at least 44x44 at {width}x{height}',page.locator('#grid [data-complete-task]').first.evaluate('(e)=>e.offsetWidth>=44&&e.offsetHeight>=44'))
   page.set_viewport_size({'width':1194,'height':834});page.wait_for_timeout(220)
   page.screenshot(path=str(SHOTS/'todo_client_v011.png'))
   check('No title/date editors in client',page.locator('input,select,textarea,[contenteditable=true]').count()==0)
   page.close()
   page=browser.new_page(viewport={'width':1440,'height':1000},has_touch=True)
   page.on('pageerror',lambda e:errors.append(str(e)))
   page.set_content((ROOT/'previews/manager_preview.html').read_text('utf-8'),wait_until='domcontentloaded');page.wait_for_selector('#previewFrame')
   frame=page.locator('#previewFrame').element_handle().content_frame();frame.wait_for_selector('.todo-week-strip')
   width=page.locator('#previewBox').bounding_box()['width']
   frame.locator('#grid [data-complete-task="task-1"]').tap()
   page.wait_for_function("RoomManager.getState().tasks.find(t=>t.id==='task-1').completed")
   check('Embedded preview completion reaches manager demo data')
   expect(frame.locator('#grid [data-complete-task="task-1"]')).to_have_attribute('aria-checked','true')
   check('Embedded preview receives authoritative saved state')
   frame.locator('#grid [data-complete-task="task-1"]').tap()
   page.wait_for_function("!RoomManager.getState().tasks.find(t=>t.id==='task-1').completed")
   check('Embedded preview undo reaches manager')
   page.wait_for_timeout(750)
   check('Previous preview-size fix is preserved',abs(page.locator('#previewBox').bounding_box()['width']-width)<.1)
   page.evaluate("RoomManager.navigate('tasks')");page.wait_for_selector('#taskRows')
   check('Manager task table reflects preview undo',not page.locator('[data-op=complete][data-id=task-1]').evaluate("e=>e.classList.contains('done')"))
   check('No uncaught browser errors',not errors,errors)
   browser.close()
 finally:
  out={'scope':'Chromium with rebuilt standalone HTML, including embedded demo; not physical iPad/Safari','checks':len(results),'passed':sum(t['passed'] for t in results),'errors':errors,'results':results}
  (REPORTS/'todo-browser-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),'utf-8')
  print(json.dumps({k:out[k] for k in ['checks','passed','errors']},ensure_ascii=False))
if __name__=='__main__':run()
