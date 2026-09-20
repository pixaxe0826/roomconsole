"""Production widget code in standalone Chromium fixture. No actual LLM inference."""
import json, os, sys, shutil
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];SHOTS=ROOT/'artifacts/screenshots';OUT=ROOT/'artifacts/test-results';SHOTS.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)

def run():
 result={'scope':'Chromium standalone exact source, synthetic LLM data; not physical Safari/V35.','checks':[],'page_errors':[]}
 def check(name,ok):
  assert ok,name
  result['checks'].append(name);print('PASS',name,flush=True)
 with sync_playwright() as p:
  b=p.chromium.launch(executable_path=(os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome')),args=['--no-sandbox'])
  pg=b.new_page(viewport={'width':1194,'height':834},has_touch=True)
  pg.on('pageerror',lambda e:result['page_errors'].append(str(e)))
  def boot():
   pg.goto('about:blank');pg.set_content((ROOT/'previews/llm_widget_preview.html').read_text('utf-8'),wait_until='domcontentloaded');pg.wait_for_selector('#grid [data-llmr-output]')
  def widget():return pg.locator('#focus [data-llmr-selected]') if pg.locator('#focus').is_visible() else pg.locator('#grid [data-llmr-selected]')
  def action(a):widget().locator(f'[data-llmr-action={a}]').click();pg.wait_for_timeout(70)
  def selected():return widget().get_attribute('data-llmr-selected')
  boot()
  check('5 widgets in example only; LLM default 4x2',pg.evaluate("RoomDisplay.getState().layout.widgets.find(w=>w.id==='llm').w===4&&RoomDisplay.getState().layout.widgets.find(w=>w.id==='llm').h===2"))
  check('latest record selected by default',selected()=='demo-llm-3')
  check('one request and one response per view',widget().locator('[data-llmr-input]').count()==1 and widget().locator('[data-llmr-output]').count()==1)
  check('no client editing inputs',pg.locator('input,textarea,select,[contenteditable=true]').count()==0)
  check('next disabled at newest',widget().locator('[data-llmr-action=newer]').is_disabled())
  pg.screenshot(path=str(SHOTS/'llm_widget.png'))
  action('older');check('left arrow selects older without expanding',selected()=='demo-llm-2' and pg.locator('#focus').is_hidden())
  action('older');check('all historical entries reachable and boundary disabled',selected()=='demo-llm-1' and widget().locator('[data-llmr-action=older]').is_disabled())
  action('newer');check('right arrow selects newer',selected()=='demo-llm-2')
  action('expand');check('expand preserves selected pair',selected()=='demo-llm-2' and pg.locator('#focus').is_visible())
  pg.screenshot(path=str(SHOTS/'llm_widget_expanded.png'))
  pg.locator('#backButton').tap();pg.wait_for_timeout(60);check('back preserves selected pair',selected()=='demo-llm-2')
  pg.evaluate("""(()=>{const s=RoomDisplay.getState(),x={...s.llm_display.items[2],id:'new-4',sent_at:'2026-09-17T01:00:00+00:00',updated_at:'2026-09-17T01:00:00+00:00'};s.llm_display.items.push(x);s.llm_display.total=4;s.llm_display_demo_details[x.id]={...s.llm_display_demo_details['demo-llm-3'],...x,input:'새 요청 4',output:'새 응답 4'};s.revision++;RoomDisplay.home()})()""")
  pg.wait_for_timeout(90);check('new entry does not steal historical selection',selected()=='demo-llm-2' and '/ 4' in widget().locator('.llmr-counter').inner_text())
  action('latest');check('latest shortcut selects most recent',selected()=='new-4')
  pg.evaluate("""(()=>{const s=RoomDisplay.getState(),x={...s.llm_display.items.at(-1),id:'new-5',updated_at:'2026-09-17T01:01:00+00:00'};s.llm_display.items.push(x);s.llm_display.total=5;s.llm_display_demo_details[x.id]={...s.llm_display_demo_details['new-4'],...x,input:'새 요청 5',output:'새 응답 5'};s.revision++;RoomDisplay.home()})()""")
  pg.wait_for_timeout(90);check('latest mode follows new entries',selected()=='new-5')
  pg.evaluate("""(()=>{const s=RoomDisplay.getState(),id='new-5';s.llm_display_demo_details[id].input='<img src=x onerror=window.HACKED=true>';s.llm_display_demo_details[id].output='<script>window.HACKED=true</script>\\n'+('긴 답변 한 줄. '.repeat(20)+'\\n').repeat(55);s.llm_display.items.at(-1).updated_at='new';RoomDisplay.home()})()""")
  pg.wait_for_timeout(100);check('untrusted input/output remain text',widget().locator('img,script').count()==0 and not pg.evaluate('!!window.HACKED'))
  action('expand');output=widget().locator('[data-llmr-scroll=output]');check('long full output scrolls without truncation',output.evaluate('(e)=>e.scrollHeight>e.clientHeight*2'))
  output.evaluate('(e)=>e.scrollTop=550');pg.evaluate("RoomDisplay.openWidget('llm')");pg.wait_for_timeout(120)
  check('long output scroll preserved on redraw',widget().locator('[data-llmr-scroll=output]').evaluate('(e)=>e.scrollTop')>=540)
  action('older');check('different response starts its own scroll',widget().locator('[data-llmr-scroll=output]').evaluate('(e)=>e.scrollTop')==0)
  # Run each response shape as latest and force a fresh indexed version.
  for n,(status,kind,out,refusal,expected) in enumerate([
   ('running','empty',None,None,'처리'),('failed','empty',None,None,'받지 못'),('cancelled','empty',None,None,'취소'),
   ('interrupted','empty',None,None,'중단'),('succeeded','tool_only',None,None,'실행하지'),
   ('succeeded','no_final_output',None,None,'최종 답변'),('succeeded','empty','',None,'비어'),
   ('succeeded','refusal',None,'모의 거절문','모의 거절문')]):
   pg.evaluate("""([status,kind,out,refusal,n])=>{let s=RoomDisplay.getState();let item=s.llm_display.items.at(-1);item.status=status;item.updated_at='shape'+n;Object.assign(s.llm_display_demo_details[item.id],{status,output_kind:kind,output:out,refusal});RoomDisplay.home()}""",[status,kind,out,refusal,n]);pg.wait_for_timeout(80)
   if widget().locator('[data-llmr-action=latest]').count():action('latest')
   check('status '+status+'/'+kind,expected in widget().locator('[data-llmr-output]').inner_text())
  for w,h in [(1194,834),(1194,720),(834,1194),(390,844)]:
   pg.set_viewport_size({'width':w,'height':h});pg.wait_for_timeout(220)
   check(f'no document overflow {w}x{h}',not pg.evaluate('document.documentElement.scrollWidth>innerWidth||document.documentElement.scrollHeight>innerHeight'))
   check(f'visible navigation {w}x{h}',widget().locator('[data-llmr-action=older]').is_visible())
  pg.set_viewport_size({'width':1194,'height':834});pg.wait_for_timeout(180)
  pg.evaluate("""(()=>{let s=RoomDisplay.getState();s.llm_display.items=[];s.llm_display.total=0;RoomDisplay.home()})()""");pg.wait_for_timeout(90)
  check('empty is honest without fabricated responses','아직 처리한 요청' in widget().inner_text() and widget().locator('[data-llmr-output]').count()==0)
  check('both arrows disabled when empty',widget().locator('[data-llmr-action=older]').is_disabled() and widget().locator('[data-llmr-action=newer]').is_disabled())
  pg.screenshot(path=str(SHOTS/'llm_widget_empty.png'))
  # A new page produces independent instance navigation state.
  boot();pg.evaluate("""(()=>{let s=RoomDisplay.getState();s.layout.widgets=s.layout.widgets.filter(w=>w.id!=='calendar');s.layout.widgets.push({id:'llm-two',type:'llm-response',title:'두 번째',x:4,y:2,w:4,h:2,config:{}});RoomDisplay.home()})()""");pg.wait_for_timeout(100)
  pg.locator('#grid [data-widget-id=llm] [data-llmr-action=older]').click();pg.wait_for_timeout(100)
  check('two widget instances have independent navigation',pg.locator('#grid [data-widget-id=llm] [data-llmr-selected]').get_attribute('data-llmr-selected')=='demo-llm-2' and pg.locator('#grid [data-widget-id=llm-two] [data-llmr-selected]').get_attribute('data-llmr-selected')=='demo-llm-3')
  pg.evaluate("""(()=>{const s=RoomDisplay.getState();s.llm_display.items=s.llm_display.items.filter(i=>i.id!=='demo-llm-2');s.llm_display.total=2;RoomDisplay.home()})()""");pg.wait_for_timeout(80)
  check('deleted selection falls back to an available neighbor',pg.locator('#grid [data-widget-id=llm] [data-llmr-selected]').get_attribute('data-llmr-selected')!='demo-llm-2')
  pg.goto('about:blank');pg.set_content((ROOT/'previews/manager_preview.html').read_text('utf-8'),wait_until='domcontentloaded');pg.wait_for_selector('.kpis');pg.evaluate("RoomManager.navigate('layout')")
  check('manager discovers new folder widget',pg.locator('[data-op=add-widget][data-id=llm-response]').count()==1)
  pg.locator('[name=gridRows]').fill('8');pg.locator('[name=gridRows]').dispatch_event('change');pg.wait_for_timeout(60);pg.locator('[data-op=add-widget][data-id=llm-response]').click();pg.wait_for_timeout(80)
  check('manager new widget uses 4x2',pg.locator('[name=widgetW]').input_value()=='4' and pg.locator('[name=widgetH]').input_value()=='2')
  check('manager explains opt-in shared projection','모든 표시 기기' in pg.locator('#content').inner_text())
  pg.locator('[data-op=save-layout]').click();pg.wait_for_timeout(80)
  check('manager saves layout without reset',pg.evaluate("RoomManager.getState().layout.widgets.some(w=>w.type==='llm-response')"))
  check('no uncaught browser error',not result['page_errors'])
  b.close()
 result['passed']=len(result['checks']);(OUT/'llm-widget-browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print('TOTAL',result['passed'])
if __name__=='__main__':run()
