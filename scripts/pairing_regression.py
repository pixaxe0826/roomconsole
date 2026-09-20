"""Pairing lifecycle regression: production manager JS + real loopback API/WebSocket.

The browser document is injected with set_content. Browser network calls are
bridged through Python to the real temporary Uvicorn server; this accommodates
navigation-restricted test environments. It is NOT a physical browser/LAN E2E test.
Delays/failures and clock offset in named cases are deliberately injected.
No user DB, secrets or installed services are accessed.
"""
import argparse, asyncio, contextlib, json, os, re, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx
from playwright.async_api import async_playwright
from websockets.asyncio.client import connect
ROOT=Path(__file__).resolve().parents[1]
RESULTS=ROOT/'artifacts'/'test-results'

BRIDGE_JS='''
// Test transport only: all normal requests and server events use the real backend.
history.replaceState=()=>{};
Room.api=async(path,method='GET',body)=>{
 const r=await window.testApi(path,method,body);
 if(!r.ok){const e=Error(typeof r.data.detail==='string'?r.data.detail:`HTTP ${r.status}`);e.status=r.status;throw e;}
 return r.data;
};
let connectionId=0;window.testConnections={};
Room.connect=(role,handler,status)=>{
 const id=++connectionId;testConnections[id]={handler,status};window.testConnect(id,role);
 return {send:msg=>window.testSend(id,msg),close:()=>{delete testConnections[id];window.testClose(id);}};
};
window.testEvent=(id,msg)=>testConnections[id]?.handler(msg);
window.testStatus=(id,ok)=>testConnections[id]?.status?.(ok);
'''

def html_source(js):
 html=(ROOT/'web/manager.html').read_text('utf-8')
 html=re.sub(r'<script defer[^>]*></script>','',html)
 html=re.sub(r'<link\b[^>]*>','',html)
 css='\n'.join((ROOT/'web'/f).read_text('utf-8') for f in ['base.css','manager.css'])
 code=(ROOT/'web/shared.js').read_text('utf-8')+'\n'+BRIDGE_JS+'\n'+(ROOT/'web/manager-llm.js').read_text('utf-8')+'\n'+js
 return html.replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+code.replace('</script','<\\/script')+'</script></body>')

class Harness:
 def __init__(self,browser,base,key):
  self.browser,self.base,self.key=browser,base,key
  self.client=httpx.AsyncClient(base_url=base,headers={'X-Room-Request':'1'},timeout=10)
  self.sockets={};self.readers=[];self.errors=[];self.overview_delay=0;self.pair_delay=0;self.fail_pair=False
  self.pair_calls=0;self.overview_calls=0;self.qr_calls=0;self.events=[]
 async def start(self,js):
  self.page=await self.browser.new_page(viewport={'width':1440,'height':1000})
  self.page.on('pageerror',lambda e:self.errors.append(str(e)))
  await self.page.expose_function('testApi',self.api)
  await self.page.expose_function('testConnect',self.open_ws)
  await self.page.expose_function('testSend',self.send_ws)
  await self.page.expose_function('testClose',self.close_ws)
  # Relative image URLs resolve against this synthetic page's explicit base URL.
  await self.page.route('**/api/admin/qr?**',self.qr)
  html=html_source(js).replace('<head>',f'<head><base href="{self.base}/">',1)
  await self.page.set_content(html,wait_until='domcontentloaded')
  await self.page.locator('#loginForm [name=token]').fill(self.key)
  await self.page.locator('#loginForm button').click()
  await self.page.wait_for_selector('.kpis')
  await self.page.wait_for_function("document.querySelector('#managerStatus').textContent.includes('실시간 연결')")
  await self.page.evaluate("RoomManager.navigate('devices')")
  await self.page.locator('#pairForm [name=baseUrl]').fill(self.base)
 async def api(self,path,method='GET',body=None):
  if path=='/api/devices/pair':
   self.pair_calls+=1
   if self.fail_pair:
    await asyncio.sleep(.1);return {'ok':False,'status':503,'data':{'detail':'Injected test service unavailable'}}
  response=await self.client.request(method,path,json=body if body is not None else None)
  if path=='/api/admin/overview':
   self.overview_calls+=1
   if self.overview_delay:await asyncio.sleep(self.overview_delay)
  if path=='/api/devices/pair' and self.pair_delay:await asyncio.sleep(self.pair_delay)
  data=None if response.status_code==204 else response.json()
  return {'ok':response.is_success,'status':response.status_code,'data':data}
 async def qr(self,route):
  self.qr_calls+=1
  url=route.request.url
  response=await self.client.get(url)
  await route.fulfill(status=response.status_code,content_type='image/svg+xml',body=response.content)
 async def open_ws(self,id,role):
  cookie='; '.join(f'{k}={v}' for k,v in self.client.cookies.items())
  ws=await connect(self.base.replace('http:','ws:')+'/ws/'+role,additional_headers={'Cookie':cookie})
  self.sockets[id]=ws
  await self.page.evaluate('([id,ok])=>testStatus(id,ok)',[id,True])
  async def read():
   try:
    async for raw in ws:
     msg=json.loads(raw);self.events.append(msg.get('type'))
     await self.page.evaluate('([id,msg])=>testEvent(id,msg)',[id,msg])
   except Exception:pass
  self.readers.append(asyncio.create_task(read()))
 async def send_ws(self,id,msg):
  if id in self.sockets:await self.sockets[id].send(json.dumps(msg))
 async def close_ws(self,id):
  if id in self.sockets:await self.sockets.pop(id).close()
 async def pair(self):
  await self.page.locator('#pairForm button[type=submit]').click()
  await self.page.wait_for_selector('#pairLink')
  await self.page.wait_for_function("!document.querySelector('#pairForm button[type=submit]').disabled")
  return await self.page.locator('#pairLink').input_value()
 async def pulse(self):
  response=await self.client.post('/api/tasks',json={'title':'Synthetic pairing refresh test','date':'2026-09-17'})
  assert response.status_code==201
  await self.page.wait_for_timeout(450)
 async def close(self):
  for ws in list(self.sockets.values()):
   with contextlib.suppress(Exception):await ws.close()
  for task in self.readers:task.cancel()
  await asyncio.gather(*self.readers,return_exceptions=True)
  await self.page.close();await self.client.aclose()

async def run(args):
 checks=[]
 def check(name,value=True):
  if not value:raise AssertionError(name)
  checks.append(name);print('PASS',name,flush=True)
 with tempfile.TemporaryDirectory() as tmp:
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  base=f'http://127.0.0.1:{port}';key='synthetic-room-pair-regression-admin-123456789'
  env={**os.environ,'HUB_DATA_DIR':tmp,'HUB_ADMIN_TOKEN':key,'HUB_INGEST_TOKEN':'synthetic-room-pair-regression-ingest-123456789'}
  with open(Path(tmp)/'server.log','w+') as log:
   server=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),'--workers','1'],cwd=ROOT,env=env,stdout=log,stderr=log)
   try:
    async with httpx.AsyncClient(base_url=base) as c:
     for _ in range(80):
      try:
       if (await c.get('/healthz')).status_code==200:break
      except httpx.RequestError:pass
      await asyncio.sleep(.1)
     else:raise RuntimeError('Test server startup failed')
    async with async_playwright() as p:
     browser=await p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox','--disable-dev-shm-usage'])
     h=Harness(browser,base,key)
     try:
      js=Path(args.baseline).read_text('utf-8') if args.baseline else (ROOT/'web/manager.js').read_text('utf-8')
      await h.start(js);page=h.page
      h.overview_delay=.6
      await page.evaluate('''()=>{
       window.pairSamples=[];
       const record=()=>{const present=!!document.querySelector('#pairLink');
        if(!pairSamples.length||pairSamples.at(-1).present!==present)pairSamples.push({ms:Math.round(performance.now()),present});};
       new MutationObserver(record).observe(document.querySelector('#content'),{childList:true,subtree:true});record();
      }''')
      await page.locator('#pairForm button').click();await page.wait_for_timeout(1800)
      samples=await page.evaluate('pairSamples')
      if args.baseline:
       check('Old code reproduces QR/link removal on real server invalidate (600ms overview delay)',[s['present'] for s in samples]==[False,True,False])
       result={'mode':'before-fix','checks':checks,'samples':samples,'errors':h.errors}
      else:
       check('QR/link survives own pair_created invalidate',await page.locator('#pairLink').count()==1)
       check('No intermediate disappear on same-tab refresh',[s['present'] for s in samples]==[False,True])
       check('Real server WebSocket invalidation was received','invalidate' in h.events)
       await page.wait_for_function("document.querySelector('#pairResult img').complete && document.querySelector('#pairResult img').naturalWidth>0")
       check('QR SVG from authenticated real backend loads')
       check('Single pair request for initial submit',h.pair_calls==1)
       url=await page.locator('#pairLink').input_value()
       await page.evaluate("window.originalQr=document.querySelector('#pairResult img')")
       h.overview_delay=0
       await page.locator('#pairForm [name=name]').fill('침실 iPad 테스트')
       await page.locator('#pairForm [name=name]').blur()
       for _ in range(4):await h.pulse()
       check('Unrelated live task changes keep same QR image node',await page.evaluate("originalQr===document.querySelector('#pairResult img')"))
       check('Typed device name survives updates',await page.locator('#pairForm [name=name]').input_value()=='침실 iPad 테스트')
       check('No extra pair created by refresh',h.pair_calls==1)
       check('QR did not redownload on live updates',h.qr_calls==1)
       before=h.overview_calls
       await page.wait_for_timeout(args.hold_seconds*1000)
       check('Pair link survives real periodic refresh',await page.locator('#pairLink').input_value()==url and h.overview_calls>before)
       check('Periodic refresh keeps image attached',await page.evaluate("originalQr===document.querySelector('#pairResult img')"))
       check('QR downloaded only once during hold',h.qr_calls==1)
       for _ in range(3):
        await page.evaluate("RoomManager.navigate('settings');RoomManager.navigate('devices')")
       check('Tab round trips restore same valid link',await page.locator('#pairLink').input_value()==url)
       check('Draft name and URL survive tab change',await page.locator('#pairForm [name=name]').input_value()=='침실 iPad 테스트' and await page.locator('#pairForm [name=baseUrl]').input_value()==base)
       # Consume the link with a distinct real display session.
       from urllib.parse import urlsplit,parse_qs
       code=parse_qs(urlsplit(url).fragment)['pair'][0]
       async with httpx.AsyncClient(base_url=base,headers={'X-Room-Request':'1'}) as display:
        response=await display.post('/api/devices/claim',json={'code':code});check('Real display can claim saved link',response.status_code==200)
        await page.wait_for_selector('[data-pair-status=paired]')
        check('Consumed link shows connected status, not blank area',await page.locator('#pairLink').count()==0 and '화면이 연결되었습니다' in await page.locator('#pairResult').inner_text())
        check('Pair code remains one-time', (await display.post('/api/devices/claim',json={'code':code})).status_code==401)
        check('Paired display retains limited permission',(await display.post('/api/tasks',json={'title':'denied','date':'2026-09-17'})).status_code==401)
       # Force the opposite ordering: POST returns after invalidate while tab changes.
       h.pair_delay=.7;before=h.pair_calls
       await page.locator('#pairForm button').click()
       await page.wait_for_function("document.querySelector('#pairForm button').disabled")
       await page.evaluate("document.querySelector('#pairForm').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
       await page.evaluate("RoomManager.navigate('tasks')")
       await page.wait_for_timeout(1000)
       await page.evaluate("RoomManager.navigate('devices')")
       await page.wait_for_selector('#pairLink')
       check('Response after tab navigation restores safely',await page.locator('#pairLink').count()==1)
       check('Pending double submit makes only one pair',h.pair_calls==before+1)
       h.pair_delay=0
       url=await page.locator('#pairLink').input_value()
       h.fail_pair=True;await page.locator('#pairForm button').click();await page.wait_for_timeout(250)
       check('Failure retains prior valid QR',await page.locator('#pairLink').input_value()==url)
       check('Failure restores button and shows error',not await page.locator('#pairForm button').is_disabled() and 'Injected' in await page.locator('#pairError').inner_text())
       h.fail_pair=False;url=await h.pair();await page.wait_for_timeout(350)
       check('Retry succeeds and clears error',await page.locator('#pairError').inner_text()=='')
       # Simulated clock offset, not a 10-minute wall-time soak.
       await page.evaluate('window.oldDateNow=Date.now;Date.now=()=>oldDateNow()+601000')
       await page.wait_for_selector('[data-pair-status=expired]')
       check('Expired link replaced by explicit message',await page.locator('#pairLink').count()==0 and await page.locator('#pairResult img').count()==0)
       await page.evaluate('Date.now=oldDateNow')
       url=await h.pair()
       data=(await h.client.get('/api/admin/overview')).json()
       did=data['devices'][0]['id']
       # ID returned by latest creation is discoverable by unique device name + latest audit.
       created=[d for d in data['devices'] if d['name']=='침실 iPad 테스트' and not d['revoked']]
       did=created[0]['id']
       response=await h.client.delete('/api/devices/'+did);check('Real revoke endpoint succeeds',response.status_code==200)
       await page.wait_for_selector('[data-pair-status=revoked]')
       check('Revoked link removed with explanation',await page.locator('#pairLink').count()==0)
       # Logout invalidates the pending response generation.
       h.pair_delay=.8;await page.locator('#pairForm button').click();await page.wait_for_timeout(100)
       await page.locator('[data-op=logout]').click();await page.wait_for_timeout(1000)
       check('Logout clears pairing secret and ignores late result',await page.locator('#login').is_visible() and await page.locator('#pairLink').count()==0)
       check('No uncaught JS errors across regression cases',not h.errors)
       check('New manager cache key served',(await h.client.get('/manager')).text.find('manager.js?v=0.1.6-llm-widget1')>=0)
       result={'mode':'after-fix','checks':checks,'hold_seconds':args.hold_seconds,'page_errors':h.errors,'initial_samples':samples}
     finally:await h.close();await browser.close()
   finally:
    server.terminate()
    try:server.wait(timeout=5)
    except subprocess.TimeoutExpired:server.kill();server.wait()
 result['passed']=len(checks)
 result['scope']='Chromium set_content with production JS; real temporary loopback API/WebSocket via Python transport bridge. Delayed responses, failure and expiry are controlled tests. Not native V35/iPad/Safari or camera scanning.'
 RESULTS.mkdir(parents=True,exist_ok=True)
 output=Path(args.output) if args.output else RESULTS/('pairing-before.json' if args.baseline else 'pairing-results.json')
 output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'passed':len(checks),'report':str(output)}),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--baseline');parser.add_argument('--hold-seconds',type=float,default=35);parser.add_argument('--output');args=parser.parse_args()
 asyncio.run(run(args))
