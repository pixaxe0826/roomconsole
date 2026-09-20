"""Real TLS/nginx/WS + bridged Chromium MediaRecorder + real FFmpeg + MOCK ASR CLI.

No actual Whisper weights are used. This validates transports/media/lifecycle,
NOT Korean recognition accuracy, physical iPad, or performance on LG V35.
Browser LAN navigation is blocked here: set_content + Python HTTP bridge is used.
MediaRecorder is real, but its source is synthetic WebAudio, not a microphone.
Certificates, keys and recordings live only in a temporary test directory.
"""
import contextlib, importlib.util, json, math, os, socket, ssl, struct, subprocess, sys, tempfile, time, wave
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/test-results';OUT.mkdir(parents=True,exist_ok=True)
SHOTS=ROOT/'artifacts/screenshots';SHOTS.mkdir(parents=True,exist_ok=True)

def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def stop(p):
 if p is None:return
 p.terminate()
 try:p.wait(timeout=6)
 except subprocess.TimeoutExpired:p.kill();p.wait()

def run():
 checks=[];backend=None;proxy=None
 def ok(name):checks.append(name);print('PASS',name,flush=True)
 with tempfile.TemporaryDirectory(prefix='roomhub-speech-test-') as tmp:
  root=Path(tmp);data=root/'data';data.mkdir();bp,hp=port(),port()
  model=root/'mock-model.bin';model.write_bytes(b'not actual model'*256)
  cli=root/'mock-asr-cli';cli.write_text('#!'+sys.executable+'\nimport pathlib,sys,time\na=sys.argv\ntime.sleep(1.0)\npathlib.Path(a[a.index("-of")+1]+".txt").write_text("예시 전사: 내일 택배 보내기. 이 문장은 기능 검사용입니다.",encoding="utf-8")\n');cli.chmod(0o700)
  (data/'speech-config.json').write_text(json.dumps({'enabled':True,'binary':str(cli),'model':str(model),'model_name':'테스트용 모의 엔진 · 실제 음성 모델 아님','ffmpeg':'/usr/bin/ffmpeg','max_seconds':5}))
  spec=importlib.util.spec_from_file_location('setuphttps',ROOT/'deploy/termux/setup_https.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
  certdir=mod.setup(root,'192.168.0.14',port=hp)
  conf=certdir/'nginx.conf';conf.write_text(conf.read_text().replace('127.0.0.1:8088',f'127.0.0.1:{bp}')+('\nuser root;\n' if os.getuid()==0 else ''))
  wav=root/'fake-input.wav'
  with wave.open(str(wav),'wb') as w:
   w.setnchannels(1);w.setsampwidth(2);w.setframerate(48000)
   w.writeframes(b''.join(struct.pack('<h',int(4000*math.sin(i*math.tau*400/48000))) for i in range(48000*8)))
  env={**os.environ,'HUB_DATA_DIR':str(data),'HUB_ADMIN_TOKEN':'local-regression-only-admin-not-for-use','HUB_INGEST_TOKEN':'local-regression-only-ingest-not-for-use'}
  log=open(root/'runtime.log','w+')
  try:
   backend=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(bp),'--workers','1','--timeout-graceful-shutdown','4'],cwd=ROOT,env=env,stdout=log,stderr=log)
   for _ in range(70):
    try:
     if httpx.get(f'http://127.0.0.1:{bp}/healthz',timeout=.5).status_code==200:break
    except httpx.RequestError:pass
    time.sleep(.1)
   else:raise AssertionError('Backend startup failed')
   proxy=subprocess.Popen(['nginx','-p',str(certdir)+'/', '-c',str(conf),'-g','daemon off;'],stdout=log,stderr=log)
   url=f'https://127.0.0.1:{hp}';tls=ssl.create_default_context(cafile=str(certdir/'private/ca.crt'))
   with httpx.Client(base_url=url,verify=tls,headers={'X-Room-Request':'1','Origin':url},timeout=10) as c:
    for _ in range(60):
     try:
      if c.get('/healthz').status_code==200:break
     except httpx.RequestError:pass
     time.sleep(.1)
    else:raise AssertionError('Verified TLS proxy startup failed')
    ok('Real nginx reverse proxy + CA verified TLS (no TLS verification bypass in HTTP tests)')
    assert c.get('/healthz').json()['version']=='0.1.4';ok('TLS health endpoint reports 0.1.4')
    assert c.get('/api/speech/status').status_code==401;ok('HTTPS anonymous speech API denied')
    login=c.post('/api/auth/login',json={'token':env['HUB_ADMIN_TOKEN']});assert login.status_code==200
    assert 'Secure' in login.headers['set-cookie'] and 'HttpOnly' in login.headers['set-cookie'];ok('HTTPS authentication cookie hardened by isolated proxy')
    headers=c.get('/client').headers;assert 'microphone=(self)' in headers['permissions-policy'] and 'blob:' in headers['content-security-policy'];ok('Production microphone and blob-audio security headers')
    assert c.get('/roomhub-ca.cer').content==(certdir/'public/roomhub-ca.cer').read_bytes()
    for route in ['/data/https/private/ca.key','/static/../data/https/private/ca.key']:
     assert c.get(route).status_code!=200
    ok('Only public CA certificate is exposed; private key routes unavailable')
    assert c.post('/api/tasks',json={'title':'기존 할 일은 그대로 유지됩니다','date':'2026-09-19'}).status_code==201
    pair=c.post('/api/devices/pair',json={'name':'Browser test iPad'}).json()
    pairs=[pair]
    for _ in range(2):pairs.append(c.post('/api/devices/pair',json={'name':'Browser test extra'}).json())
    # Real WSS handshake/ping through the HTTPS proxy, without a browser transport shim.
    from websockets.sync.client import connect
    with connect(url.replace('https:','wss:')+'/ws/manager',ssl=tls,origin=url,additional_headers={'Cookie':'; '.join(f'{k}={v}' for k,v in c.cookies.items())}) as ws:
     assert json.loads(ws.recv())['type']=='hello';ws.send(json.dumps({'type':'ping'}));assert json.loads(ws.recv())['type']=='pong'
    ok('Real authenticated WSS handshake and application heartbeat through nginx')
    # Display session used by the bridge; deliberately not the admin session.
    d=httpx.Client(base_url=url,verify=tls,headers={'X-Room-Request':'1','Origin':url},timeout=12)
    assert d.post('/api/devices/claim',json={'code':pairs[0]['path'].split('=')[1]}).status_code==200
    def bridge(path,method,payload):
     if payload and payload.get('kind')=='form':
      import base64
      fields={};files={}
      for x in payload['items']:
       if 'bytes' in x:files[x['key']]=(x['name'],base64.b64decode(x['bytes']),x['mime'])
       else:fields[x['key']]=x['value']
      response=d.request(method,path,data=fields,files=files)
     else:response=d.request(method,path)
     return {'status':response.status_code,'body':response.text}
    transport=r'''
// TEST ONLY: browser navigation to LAN URLs is restricted in this environment.
Object.defineProperty(window,'isSecureContext',{value:true,configurable:true});
let memory={};Object.defineProperty(window,'sessionStorage',{configurable:true,value:{getItem:k=>memory[k]||null,setItem:(k,v)=>memory[k]=String(v),removeItem:k=>delete memory[k]}});
window.captureContexts=[];
Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:async()=>{
 const ctx=new AudioContext(),dest=ctx.createMediaStreamDestination(),osc=ctx.createOscillator();
 osc.frequency.value=400;osc.connect(dest);osc.start();await ctx.resume();captureContexts.push(ctx);return dest.stream;
}}});
window.fetch=async(path,options={})=>{
 let body=null;
 if(options.body instanceof FormData){body={kind:'form',items:[]};for(const [key,val] of options.body){
  if(val instanceof Blob){const bytes=new Uint8Array(await val.arrayBuffer());let raw='';for(let i=0;i<bytes.length;i+=8192)raw+=String.fromCharCode(...bytes.subarray(i,i+8192));body.items.push({key,name:val.name,mime:val.type,bytes:btoa(raw)})}
  else body.items.push({key,value:val});
 }}
 const res=await window.testSpeechHTTP(String(path),options.method||'GET',body);
 return new Response(res.body,{status:res.status,headers:{'Content-Type':'application/json'}});
};
'''
    with sync_playwright() as pw:
     browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--autoplay-policy=no-user-gesture-required'])
     ctx=browser.new_context(viewport={'width':1194,'height':834});errors=[]
     p=ctx.new_page();p.on('pageerror',lambda e:errors.append(str(e)));p.expose_function('testSpeechHTTP',bridge)
     html=(ROOT/'previews/client_preview.html').read_text()
     anchor="const $=s=>document.querySelector(s),E=Room.esc,demo=!!window.ROOM_DEMO;"
     assert anchor in html;html=html.replace(anchor,transport+"const $=s=>document.querySelector(s),E=Room.esc,demo=false;")
     p.set_content(html,wait_until='domcontentloaded');p.wait_for_selector('#grid .widget')
     p.locator('[data-open-speech]').first.click();p.wait_for_selector('#speechStart');ok('Production speech UI receives real engine readiness via explicit Python HTTPS bridge')
     p.screenshot(path=str(SHOTS/'speech-ready.png'))
     for i in range(2):
      p.locator('#speechStart').click();p.wait_for_selector('#speechStop');p.wait_for_timeout(500)
      p.locator('#speechDiscard').click();p.wait_for_selector('#speechStart');p.wait_for_timeout(150)
     assert not c.get('/api/speech/jobs').json()['jobs'];ok('Two capture cancellations do not create server jobs')
     p.locator('#speechStart').click();p.wait_for_selector('#speechStop');p.wait_for_timeout(1300)
     p.screenshot(path=str(SHOTS/'speech-recording.png'))
     p.locator('#speechStop').click();p.wait_for_selector('#speechSend');ok('REAL MediaRecorder encodes a synthetic WebAudio stream for review')
     assert p.locator('audio').get_attribute('src').startswith('blob:');ok('Preview audio stays as local Blob before explicit send')
     p.locator('#speechSend').click();p.wait_for_selector('#speechTranscript',timeout=20000)
     assert '기능 검사용' in p.locator('#speechTranscript').inner_text();ok('MediaRecorder bytes -> bridged multipart -> real FFmpeg -> MOCK CLI -> production result UI')
     p.screenshot(path=str(SHOTS/'speech-result.png'))
     job=c.get('/api/speech/jobs').json()['jobs'][0];assert job['status']=='succeeded' and job['duration']>.5
     assert len(c.get('/api/state').json()['tasks'])==1;ok('Transcription does not create any task')
     p.locator('#speechClose').click();p.locator('[data-open-speech]').first.click();p.wait_for_selector('#speechTranscript');ok('Closing and reopening resumes server result by saved job ID')
     p.set_viewport_size({'width':834,'height':1194});p.wait_for_timeout(200)
     bounds=p.locator('.speech-card').bounding_box();assert bounds['x']>=0 and bounds['y']>=0 and bounds['width']<=834
     p.screenshot(path=str(SHOTS/'speech-portrait.png'));ok('Portrait modal fits 834x1194')
     p.locator('#speechNew').click();p.wait_for_selector('#speechStart');p.locator('#speechStart').click();p.wait_for_selector('#speechStop')
     p.wait_for_selector('#speechSend',timeout=9000);ok('Configured five-second limit automatically stops actual MediaRecorder (normal default30s)')
     p.locator('#speechAgain').click();p.locator('#speechStart').click();p.wait_for_selector('#speechStop')
     p.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
     p.wait_for_selector('#speechStart');assert '숨겨져' in p.locator('#speechMessage').inner_text();ok('Injected page-hidden event discards recording, no upload')
     p.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
     p.evaluate("()=>{navigator.mediaDevices.getUserMedia=()=>Promise.reject(new DOMException('Denied','NotAllowedError'))}")
     p.locator('#speechStart').click();p.wait_for_function("document.querySelector('#speechMessage')?.textContent.includes('거부')");ok('Injected microphone refusal returns retryable UI')
     p.locator('#speechClose').click();p.evaluate("sessionStorage.removeItem('room-speech-job');Object.defineProperty(window,'isSecureContext',{value:false})")
     p.locator('[data-open-speech]').first.click();p.wait_for_selector('#speechCheck');assert 'HTTPS' in p.locator('#speechBody').inner_text();ok('Injected insecure context blocks recording with HTTPS explanation')
     assert not errors,errors;ok('Production speech UI paths have no uncaught exceptions')
     # Populate actual manager source with a fixture made from real test API results.
     mp=ctx.new_page();mhtml=(ROOT/'previews/manager_preview.html').read_text()
     # The manager is a standalone demo for this screenshot; state inserted before manager boot.
     mhtml=mhtml.replace("(() => {\n'use strict';\nconst R=Room,$=", "window.ROOM_DEMO_OVERVIEW="+json.dumps(c.get('/api/admin/overview').json(),ensure_ascii=False)+";\n(() => {\n'use strict';\nconst R=Room,$=",1)
     import re
     mhtml=re.sub(r'let demoOverview=demo\?[^\n]+;',lambda m:'let demoOverview='+json.dumps(c.get('/api/admin/overview').json(),ensure_ascii=False)+';',mhtml,count=1)
     mp.set_content(mhtml,wait_until='domcontentloaded');mp.wait_for_selector('.kpis');mp.evaluate("RoomManager.navigate('voice')");mp.wait_for_selector('.speech-engine')
     mp.screenshot(path=str(SHOTS/'speech-manager.png'),full_page=True);ok('Updated manager engine/inbox controls render in standalone demo')
     browser.close();d.close()
   result={'passed':len(checks),'checks':checks,'scope':'Real loopback TLS/HTTP/WebSocket/nginx with CA verification; production speech UI uses a Python HTTP bridge because browser LAN navigation is restricted. REAL MediaRecorder encodes synthetic WebAudio (microphone/secure-context are mocked), REAL FFmpeg, MOCK ASR CLI. Not physical iPad/V35 or actual recognition.'}
   (OUT/'speech-live-results.json').write_text(json.dumps(result,indent=2,ensure_ascii=False));print(json.dumps(result,ensure_ascii=False))
  except BaseException:
   log.flush();print((root/'runtime.log').read_text()[-6000:],file=sys.stderr)
   raise
  finally:stop(proxy);stop(backend);log.close()
if __name__=='__main__':run()
