"""Exercise the production client's completion path with a controllable transport.
Mock only HTTP/WS and module loading; use actual UI, request/response and conflict logic.
Transport is intentionally mocked; this is not real browser networking.
"""
from pathlib import Path
import json,os,shutil
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
REPORTS=ROOT/'artifacts'/'test-results';REPORTS.mkdir(parents=True,exist_ok=True)
SHOTS=ROOT/'artifacts'/'screenshots';SHOTS.mkdir(parents=True,exist_ok=True)
FIXTURE=r'''
window.TEST={server:structuredClone(window.ROOM_DEMO),requests:[],pending:[],hold:false,getFailure:false,holdGet:false,gets:[]};
TEST.snapshot=()=>structuredClone(TEST.server);
Room.api=async path=>{
 if(path!=='/api/state')throw Error('Unexpected test API '+path);
 if(TEST.getFailure)throw TypeError('simulated offline');
 if(TEST.holdGet)return new Promise(resolve=>TEST.gets.push({resolve,snapshot:TEST.snapshot()}));
 return TEST.snapshot();
};
Room.connect=(role,handler,status)=>{TEST.message=handler;TEST.status=status;queueMicrotask(()=>{status(true);handler({type:'hello'})});return {send(){},close(){}}};
window.fetch=(path,options)=>{
 if(!String(path).endsWith('/completion'))throw Error('Unexpected fetch '+path);
 const id=String(path).split('/')[3],body=JSON.parse(options.body);TEST.requests.push({id,body});
 if(TEST.networkFailure)return Promise.reject(TypeError('simulated request failed'));
 return new Promise((resolve,reject)=>{
  const finish=()=>{
   if(options.signal.aborted)return;
   let task=TEST.server.tasks.find(t=>t.id===id),status=200,out;
   if(TEST.unauthorized){status=401;out={detail:'연결 필요'}}
   else if(!task){status=404;out={detail:'삭제됨'}}
   else if(task.version!==body.version){status=409;out={detail:'충돌'}}
   else{task.completed=body.completed;task.version++;TEST.server.revision++;out=structuredClone(task)}
   resolve(new Response(JSON.stringify(out),{status,headers:{'Content-Type':'application/json'}}));
   if(status===200)queueMicrotask(()=>TEST.message({type:'invalidate'}));
  };
  options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
  if(TEST.hold)TEST.pending.push(finish);else finish();
 });
};
'''

def fixture_html():
 html=(ROOT/'previews/client_preview.html').read_text('utf-8')
 html=html.replace('const demo=!!window.ROOM_DEMO,commands=', 'const demo=false,commands=',1)
 old="modules[key]=demo?window.ROOM_WIDGETS[m.id]:await import(`${m.baseUrl}/${m.entry}?v=${encodeURIComponent(m.version)}`);"
 assert old in html;html=html.replace(old,'modules[key]=window.ROOM_WIDGETS[m.id];',1)
 html=html.replace("   if(!demo){\n    let css=", "   if(false){\n    let css=",1)
 anchor="(() => {\n'use strict';\nconst R=Room,$="
 assert anchor in html;html=html.replace(anchor,FIXTURE+'\n'+anchor,1)
 # Inline fixture still contains real 12-second abort timeout.
 return html

def run():
 results=[];errors=[];finished=False
 def check(name,ok=True,detail=None):
  results.append({'name':name,'passed':bool(ok),'detail':detail})
  print(('PASS ' if ok else 'FAIL ')+name,flush=True)
  if not ok:raise AssertionError(f'{name}: {detail}')
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox','--disable-dev-shm-usage'])
   page=browser.new_page(viewport={'width':1194,'height':834},has_touch=True);page.set_default_timeout(5000);page.on('pageerror',lambda e:errors.append(str(e)))
   def reset():
    # A new document clears timers, handlers and globals from the previous scenario.
    page.goto('about:blank');page.set_content(fixture_html(),wait_until='domcontentloaded');page.wait_for_selector('#grid [data-complete-task=task-1]');expect(page.locator('#grid [data-complete-task=task-1]')).to_be_enabled()
   reset();sel='#grid [data-complete-task=task-1]'
   page.evaluate('TEST.hold=true');page.locator(sel).tap();expect(page.locator(sel)).to_be_disabled()
   page.locator(sel).dispatch_event('click');page.locator(sel).dispatch_event('click')
   check('Pending touch sends exactly one explicit desired value',page.evaluate("TEST.requests.length===1&&TEST.requests[0].body.completed===true&&TEST.requests[0].body.version===1"))
   check('No optimistic false success before response',page.locator(sel).get_attribute('aria-checked')=='false')
   page.locator('#grid [data-week-step="1"]').tap();page.wait_for_timeout(60)
   page.evaluate('TEST.hold=false;TEST.pending.shift()()');page.wait_for_timeout(90)
   check('Acknowledgement preserves currently selected week',page.locator('#grid .day-chip.active').get_attribute('data-date')=='2026-09-24')
   page.locator('#grid [data-week-today]').tap();expect(page.locator(sel)).to_have_attribute('aria-checked','true')
   page.locator(sel).tap();expect(page.locator(sel)).to_have_attribute('aria-checked','false')
   check('Undo sends false, not a server-side blind toggle',page.evaluate('TEST.requests[1].body.completed===false&&TEST.requests[1].body.version===2'))
   reset();page.evaluate('TEST.hold=true');page.locator(sel).tap()
   page.evaluate("const t=TEST.server.tasks.find(t=>t.id==='task-1');t.title='다른 화면에서 수정';t.version++;TEST.server.revision++;TEST.pending.shift()()")
   expect(page.locator('.toast')).to_contain_text('다른 화면에서 변경');expect(page.locator(sel)).to_be_enabled()
   check('409 displays conflict and retains newer server fields',page.evaluate("RoomDisplay.getState().tasks.find(t=>t.id==='task-1').title==='다른 화면에서 수정'&&!RoomDisplay.getState().tasks.find(t=>t.id==='task-1').completed"))
   reset();page.evaluate('TEST.hold=true');page.locator(sel).tap()
   page.evaluate("TEST.server.tasks=TEST.server.tasks.filter(t=>t.id!=='task-1');TEST.server.revision++;TEST.pending.shift()()")
   expect(page.locator('.toast')).to_contain_text('삭제된 할 일');expect(page.locator(sel)).to_have_count(0)
   check('404 removes deleted occurrence without resurrection')
   reset();page.evaluate('TEST.networkFailure=true;TEST.getFailure=true');page.locator(sel).tap()
   expect(page.locator('.toast')).to_contain_text('저장 결과를 확인하지 못했어요');expect(page.locator(sel)).to_be_disabled()
   check('Network failure does not mark a task complete',page.evaluate("!TEST.server.tasks.find(t=>t.id==='task-1').completed&&!RoomDisplay.getState().tasks.find(t=>t.id==='task-1').completed"))
   check('Offline still allows date navigation',page.locator('#grid [data-week-step="1"]').is_enabled())
   count=page.evaluate('TEST.requests.length');page.locator(sel).dispatch_event('click');check('Offline does not queue writes',page.evaluate('TEST.requests.length')==count)
   page.evaluate('TEST.networkFailure=false;TEST.getFailure=false;TEST.message({type:"hello"})');expect(page.locator(sel)).to_be_enabled()
   check('Reconnect refreshes without replaying failure',page.evaluate('TEST.requests.length')==count)
   reset();page.evaluate('TEST.holdGet=true;TEST.message({type:"invalidate"})');page.wait_for_timeout(70)
   page.locator(sel).tap();expect(page.locator(sel)).to_have_attribute('aria-checked','true')
   page.evaluate('TEST.holdGet=false;for(const g of TEST.gets.splice(0))g.resolve(g.snapshot)');page.wait_for_timeout(150)
   check('In-flight older snapshot does not revert acknowledged state',page.locator(sel).get_attribute('aria-checked')=='true')
   reset();page.evaluate('TEST.hold=true');page.locator(sel).tap()
   page.wait_for_timeout(12200);expect(page.locator(sel)).to_be_enabled()
   check('12-second timeout releases busy control without false success',page.locator(sel).get_attribute('aria-checked')=='false')
   check('Timeout shows uncertain-save message','저장 결과를 확인하지 못했어요' in page.locator('.toast').inner_text())
   reset();page.evaluate('TEST.unauthorized=true');page.locator(sel).tap();page.wait_for_selector('.loading-card')
   check('401 removes actionable task UI and asks to pair again',page.locator('[data-complete-task]').count()==0)
   check('No uncaught browser exceptions',not errors,errors)
   browser.close();finished=True
 finally:
  out={'scope':'Chromium with production completion UI/logic and mocked HTTP, WS, widget transport. Not live browser networking.','completed':finished,'checks':len(results),'passed':sum(x['passed'] for x in results),'errors':errors,'results':results}
  (REPORTS/'todo-transport-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),'utf-8');print(json.dumps({k:out[k] for k in ['completed','checks','passed','errors']},ensure_ascii=False))
if __name__=='__main__':run()
