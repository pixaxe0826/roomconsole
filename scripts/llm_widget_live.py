"""Real loopback API/SQLite with a mock LLM, Chromium via explicit HTTP bridge.
Browser local navigation is blocked in the artifact environment, so this harness
inlines exact deployed modules and bridges fetch only. WS notifications are injected;
server WebSocket notifications are independently checked by backend tests.
"""
from pathlib import Path
import asyncio, contextlib, hashlib, json, os, re, socket, sys, tempfile, threading, time
import httpx, uvicorn, shutil
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
 result={'scope':__doc__,'checks':[],'page_errors':[]}
 def check(name,ok):
  assert ok,name
  result['checks'].append(name);print('PASS',name,flush=True)
 class Backend:
  def __init__(self):self.hold=threading.Event();self.calls=0
  async def generate(self,endpoint,body,timeout):
   self.calls+=1
   while self.hold.is_set():await asyncio.sleep(.01)
   response={'model':'MOCK ONLY','choices':[{'message':{'content':'[모의] 입력 확인. <img src=x onerror=window.HACKED=true>','reasoning_content':'SECRET_INTERNAL'},'finish_reason':'stop'}]}
   return response,json.dumps(response)
 backend=Backend()
 with tempfile.TemporaryDirectory() as td:
  os.environ['HUB_DATA_DIR']=td+'/import-only'
  from app.main import create_app
  app=create_app(Path(td)/'data',weather_enabled=False,llm_backend=backend)
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  base=f'http://127.0.0.1:{port}'
  server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False))
  th=threading.Thread(target=server.run,daemon=True);th.start()
  for _ in range(100):
   if server.started:break
   time.sleep(.03)
  admin=httpx.Client(base_url=base,trust_env=False,headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'},timeout=15)
  display=httpx.Client(base_url=base,trust_env=False,headers={'X-Room-Request':'1'},timeout=15)
  lay=admin.get('/api/state').json()['layout'];lay['widgets'][0]['w']=2;lay['widgets'][1].update(x=2,w=2);lay['widgets'].append({'id':'llm','type':'llm-response','title':'LLM 응답','x':4,'y':0,'w':4,'h':2,'config':{}})
  assert admin.put('/api/layout',json=lay).status_code==200
  pair=admin.post('/api/devices/pair',json={'name':'bridge test'}).json();assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
  calls=[];errors={'enabled':False}
  def bridge(path,method,body):
   assert method=='GET' and path.startswith(('/api/state','/api/display/llm/','/api/speech/'))
   calls.append(path)
   if errors['enabled'] and path.startswith('/api/display/llm/'):
    return {'status':503,'body':'{"detail":"test transient failure"}'}
   r=display.request(method,path)
   return {'status':r.status_code,'body':r.text}
  def wait_done(rid):
   end=time.monotonic()+5
   while time.monotonic()<end:
    r=admin.get('/api/llm/requests/'+rid).json()
    if r['status']=='succeeded':return r
    time.sleep(.03)
   raise AssertionError('Mock result missing')
  def submit(n):
   text=f'요청 {n}';v=admin.post('/api/voice/text',json={'request_id':f'voice-live-{n}','source':'live-test','text':text}).json()['id']
   return admin.post('/api/llm/requests',json={'request_id':f'llm-live-{n}','voice_id':v,'expected_text_sha256':hashlib.sha256(text.encode()).hexdigest()}).json()['id']
  try:
   with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path=(os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome')),args=['--no-sandbox'])
    pg=browser.new_page(viewport={'width':1194,'height':834});pg.on('pageerror',lambda e:result['page_errors'].append(str(e)));pg.expose_function('llmWidgetHTTP',bridge)
    html=(ROOT/'web/client.html').read_text();html=re.sub(r'<link\b[^>]*>','',html);html=re.sub(r'<script defer[^>]*></script>','',html)
    css='\n'.join((ROOT/'web'/x).read_text() for x in ['base.css','client.css','speech.css'])+'\n'+'\n'.join(p.read_text() for p in (ROOT/'widgets').glob('*/style.css'))
    code=(ROOT/'web/shared.js').read_text()+'''\nwindow.fetch=(path,options={})=>new Promise((resolve,reject)=>{
      if(options.signal?.aborted){reject(new DOMException('Aborted','AbortError'));return;}
      options.signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
      window.llmWidgetHTTP(String(path),options.method||'GET',options.body||null).then(r=>resolve(new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}}))).catch(reject);
    });
    Room.connect=(role,handler,status)=>{window.widgetInvalidate=()=>handler({type:'invalidate'});window.widgetOffline=()=>status(false);queueMicrotask(()=>{status(true);handler({type:'hello'})});return {send:()=>{},close:()=>{}};};
    window.ROOM_WIDGETS={};
    '''
    for mod in (ROOT/'widgets').glob('*/widget.js'):
     text=mod.read_text().replace('export function','function');names='render,bind' if 'function bind(' in text else 'render'
     code+=f'window.ROOM_WIDGETS[{json.dumps(mod.parent.name)}]=(()=>{{{text}\nreturn {{{names}}};}})();\n'
    client=(ROOT/'web/client.js').read_text();client=client.replace('demo?window.ROOM_WIDGETS[m.id]:await import(`${m.baseUrl}/${m.entry}?v=${encodeURIComponent(m.version)}`)','window.ROOM_WIDGETS[m.id]')
    code+=client
    html=html.replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+code.replace('</script','<\\/script')+'</script></body>')
    pg.set_content(html,wait_until='domcontentloaded');pg.wait_for_selector('.llmr-empty')
    check('read-only paired display loads without admin key', '처리한 요청이 없어요' in pg.locator('.llmr-content').inner_text())
    rid=submit(1);pg.evaluate('widgetInvalidate()');pg.wait_for_timeout(120)
    check('prepared remains hidden with no backend call',backend.calls==0 and pg.locator('[data-llmr-input]').count()==0)
    cfg=admin.get('/api/llm/config').json()['config'];cfg['enabled']=True;assert admin.put('/api/llm/config',json=cfg).status_code==200
    backend.hold.set();assert admin.post('/api/llm/requests/'+rid+'/send').status_code==200
    time.sleep(.08);pg.evaluate('widgetInvalidate()');pg.wait_for_selector('[data-llmr-input]')
    check('actual attempted request input rendered',pg.locator('[data-llmr-input]').inner_text()=='요청 1')
    check('running state shows no fake output','처리' in pg.locator('[data-llmr-output]').inner_text())
    backend.hold.clear();wait_done(rid);pg.evaluate('widgetInvalidate()');pg.locator('[data-llmr-output]').filter(has_text='[모의]').wait_for()
    check('real API result reaches browser', '[모의]' in pg.locator('[data-llmr-output]').inner_text())
    check('response HTML is escaped',pg.locator('.llmr-content img').count()==0 and not pg.evaluate('!!window.HACKED'))
    check('reasoning is not exposed', 'SECRET_INTERNAL' not in pg.content())
    n_before=backend.calls;pg.locator('[data-llmr-action=expand]').click();pg.locator('#backButton').click();pg.wait_for_timeout(150)
    check('UI navigation never invokes LLM',backend.calls==n_before)
    # Re-rendering of the same exact indexed item reuses the one selected detail.
    count=sum(x.startswith('/api/display/llm/') for x in calls)
    for _ in range(4):pg.evaluate('RoomDisplay.home()');pg.wait_for_timeout(40)
    check('re-render does not generate fetch loop',sum(x.startswith('/api/display/llm/') for x in calls)==count)
    errors['enabled']=True
    rid2=submit(2);wait_done(rid2);pg.evaluate('widgetInvalidate()');pg.wait_for_selector('[data-llmr-action=retry]')
    count=len(calls);pg.wait_for_timeout(350)
    check('failed detail GET does not retry infinitely',len(calls)==count)
    check('failed request not paired with previous output',pg.locator('[data-llmr-output]').count()==0)
    errors['enabled']=False;pg.locator('[data-llmr-action=retry]').click();pg.wait_for_selector('[data-llmr-output]')
    check('explicit retry recovers read-only detail',pg.locator('[data-llmr-input]').inner_text()=='요청 2')
    pg.locator('[data-llmr-action=older]').click();pg.wait_for_timeout(100)
    check('historical pair loaded by id',pg.locator('[data-llmr-input]').inner_text()=='요청 1')
    rid3=submit(3);wait_done(rid3);pg.evaluate('widgetInvalidate()');pg.wait_for_timeout(160)
    check('new entry does not replace historical selection',pg.locator('[data-llmr-input]').inner_text()=='요청 1')
    pg.locator('[data-llmr-action=latest]').click();pg.wait_for_timeout(120)
    check('latest shortcut reads last real record',pg.locator('[data-llmr-input]').inner_text()=='요청 3')
    # Simulate an out-of-order completed read after a fast navigation change.
    # Backend is real; only the delivery of this one browser response is delayed.
    pg.evaluate("""rid=>{window.originalWidgetFetch=window.fetch;window.fetch=async(p,o)=>{const r=await originalWidgetFetch(p,o);if(String(p).endsWith(rid))await new Promise(resolve=>setTimeout(resolve,500));return r;};}""",rid)
    pg.locator('[data-llmr-action=older]').click();pg.wait_for_timeout(60)
    pg.locator('[data-llmr-action=older]').click();pg.wait_for_timeout(50)
    pg.locator('[data-llmr-action=newer]').click();pg.wait_for_timeout(700)
    check('late previous response cannot overwrite newer selection',pg.locator('[data-llmr-input]').inner_text()=='요청 2')
    pg.evaluate('()=>{window.fetch=window.originalWidgetFetch;}')
    # Simulate a stalled GET; use the production ten-second AbortController timer.
    rid4=submit(4);wait_done(rid4)
    pg.evaluate("""rid=>{window.fetch=(p,o={})=>String(p).endsWith(rid)?new Promise((resolve,reject)=>o.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true})):originalWidgetFetch(p,o);}""",rid4)
    pg.locator('[data-llmr-action=latest]').click();pg.evaluate('widgetInvalidate()');pg.wait_for_selector('[data-llmr-action=retry]',timeout=13000)
    check('ten-second read timeout shows recovery control','초과' in pg.locator('.llmr-content').inner_text())
    check('timed-out view is not indefinitely busy',pg.locator('[data-llmr-selected]').get_attribute('aria-busy')=='false')
    pg.evaluate('()=>{window.fetch=window.originalWidgetFetch;}');pg.locator('[data-llmr-action=retry]').click();pg.wait_for_selector('[data-llmr-output]')
    check('retry after timeout reads same request without re-generation',backend.calls==4 and pg.locator('[data-llmr-input]').inner_text()=='요청 4')
    pg.evaluate('widgetOffline()');pg.wait_for_timeout(100)
    check('disconnect labels stale content','연결 끊김' in pg.locator('.llmr-content').inner_text())
    (ROOT/'artifacts/screenshots').mkdir(parents=True,exist_ok=True)
    pg.screenshot(path=str(ROOT/'artifacts/screenshots/llm_widget_live_fixture.png'))
    # Revocation on the server is not ignored just because the widget is loaded.
    assert admin.delete('/api/devices/'+pair['device_id']).status_code==200
    pg.evaluate('widgetInvalidate()');pg.wait_for_selector('.loading-card')
    check('revoked session clears displayed content',pg.locator('[data-llmr-input]').count()==0 and '연결해' in pg.locator('.loading-card').inner_text())
    check('model called only for explicit manager sends',backend.calls==4)
    check('no automatic task execution',admin.get('/api/state').json()['tasks']==[])
    check('no uncaught JavaScript error',not result['page_errors'])
    browser.close()
  finally:
   backend.hold.clear();server.should_exit=True;th.join(timeout=5);admin.close();display.close()
 result['passed']=len(result['checks']);out=ROOT/'artifacts/test-results/llm-widget-live.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print('TOTAL',result['passed'])
if __name__=='__main__':main()
