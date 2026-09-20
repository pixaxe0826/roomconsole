"""Legacy-mode loopback HTTP + Chromium regression; MOCK LLM, no real model.
Use HUB_BROWSER_BRIDGE=1 when browser local navigation is blocked.
Temporary data only. Never run against the user's operational V35 DB.
"""
from __future__ import annotations
import asyncio, contextlib, hashlib, json, os, socket, sys, tempfile, threading, time, re
import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import uvicorn
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def main():
    result={'scope':'Real loopback HTTP/SQLite/Chromium; MOCK LLM responses, no LLM installation/inference','checks':[],'page_errors':[]}
    def check(name,condition):
        if not condition:raise AssertionError(name)
        result['checks'].append(name);print('PASS',name,flush=True)
    captured=[];mode={'usage':True};out='[검증용 모의 응답 · 실제 모델 아님]\n내일 오후 3시에 택배를 보내려는 요청입니다. 할 일을 자동으로 추가하지 않았습니다.\n<script>window.PWNED=true</script>'
    class Backend(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def send_json(self,obj):
            data=json.dumps(obj,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers()\n            try:self.wfile.write(data)\n            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):self.send_json({'data':[{'id':'Qwen3-0.6B-Q5_K_M.gguf'}]})
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']));captured.append(body)
            time.sleep(.3)
            response={'model':'MOCK Qwen-compatible fixture','choices':[{'message':{'role':'assistant','content':out},'finish_reason':'stop'}]}
            if mode['usage']:response.update(usage={'prompt_tokens':132,'completion_tokens':48,'total_tokens':180},timings={'prompt_ms':100.,'predicted_n':48,'predicted_ms':2000.,'predicted_per_second':24.})
            self.send_json(response)
    backend=ThreadingHTTPServer(('127.0.0.1',0),Backend);threading.Thread(target=backend.serve_forever,daemon=True).start()
    td=tempfile.TemporaryDirectory();os.environ['HUB_DATA_DIR']=td.name+'/unused-default'
    from app.main import create_app
    app=create_app(Path(td.name)/'data',weather_enabled=False)
    port=free_port();base=f'http://127.0.0.1:{port}'
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning',access_log=False))
    th=threading.Thread(target=server.run,daemon=True);th.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.03)
    artifacts=ROOT/'artifacts';(artifacts/'screenshots').mkdir(parents=True,exist_ok=True);(artifacts/'test-results').mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            context=browser.new_context(viewport={'width':1440,'height':1100});page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
            headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}
            api=context.request
            def request(method,path,data=None):return getattr(api,method)(base+path,headers=headers,data=data)
            source='내일 오후 세 시에 택배 보내기 추가해 줘.'
            vid=request('post','/api/voice/text',{'request_id':'browser-voice','source':'UI-test','text':source}).json()['id']
            check('health/version 0.1.6',request('get','/healthz').json()['version']=='0.1.6')
            browser_http=None
            injected_html=None
            if os.environ.get('HUB_BROWSER_BRIDGE')=='1':
                result['scope']='Chromium set_content + explicit Python HTTP bridge to real loopback server; browser WebSocket simulated; MOCK LLM backend, no model inference'
                browser_http=httpx.Client(base_url=base,trust_env=False,headers={'X-Room-Request':'1'},timeout=15)
                def bridge(path,method,body):
                    res=browser_http.request(method,path,json=body)
                    return {'status':res.status_code,'body':res.text}
                page.expose_function('testLLMHTTP',bridge)
                html=(ROOT/'web/manager.html').read_text('utf-8')
                html=re.sub(r'<script defer[^>]*></script>','',html)
                html=re.sub(r'<link\b[^>]*>','',html)
                css='\n'.join((ROOT/'web'/f).read_text('utf-8') for f in ['base.css','manager.css','speech.css','manager-llm.css'])
                bridge_js="""
                history.replaceState=()=>{};
                window.fetch=async(path,options={})=>{
                 const r=await window.testLLMHTTP(String(path),options.method||'GET',options.body?JSON.parse(options.body):null);
                 return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});
                };
                Room.connect=(role,handler,status)=>{queueMicrotask(()=>{status?.(true);handler({type:'hello'})});return {send:()=>{},close:()=>{}}};
                """
                code=(ROOT/'web/shared.js').read_text('utf-8')+'\n'+bridge_js+'\n'+(ROOT/'web/manager-llm.js').read_text('utf-8')+'\n'+(ROOT/'web/manager.js').read_text('utf-8')
                injected_html=html.replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+code.replace('</script','<\\/script')+'</script></body>')
                page.set_content(injected_html,wait_until='domcontentloaded')
            else:
                page.goto(base+'/manager')
            page.locator('#loginForm input').fill(app.state.admin_token);page.locator('#loginForm button').click();page.wait_for_selector('#managerApp:not(.hidden)')
            page.evaluate("RoomLLM.setMode('legacy');RoomManager.navigate('voice')");page.get_by_role('button',name='LLM으로 전송',exact=True).click();page.wait_for_selector('.llm-history-item.selected')
            check('disabled model stores prepared request',page.locator('.llm-detail-heading').inner_text().find('미전송')>=0)
            check('disabled model never called',len(captured)==0)
            check('no fabricated output tokens',page.locator('.llm-metric').nth(1).locator('strong').inner_text().startswith('—'))
            rid=page.evaluate('RoomLLM.getSelection()')
            row=request('get','/api/llm/requests/'+rid).json();check('exact source snapshot',row['source_text']==source)
            page.locator('[data-section="payload"] summary').click();check('payload shows system and user',page.locator('[data-section="payload"] pre').inner_text().find('"role": "system"')>=0)
            page.evaluate("RoomManager.navigate('tasks')");page.evaluate("RoomManager.navigate('llm')");page.wait_for_selector('.llm-history-item.selected');check('navigation preserves selected request',page.evaluate('RoomLLM.getSelection()')==rid)
            if injected_html:
                page.goto('about:blank');page.set_content(injected_html,wait_until='domcontentloaded');page.wait_for_selector('.kpis');page.evaluate("RoomManager.navigate('llm')")
            else:page.reload()
            page.wait_for_selector('.llm-history-item.selected');check('reload rehydrates persisted history',page.locator('.llm-history-item').count()==1)
            # Configure with API so a prepared default-endpoint request correctly rejects changed endpoint.
            config=request('get','/api/llm/config').json()['config'];config.update(enabled=True,base_url=f'http://127.0.0.1:{backend.server_port}/v1');check('save local backend configuration',request('put','/api/llm/config',config).status==200)
            check('prepared old endpoint rejected',request('post','/api/llm/requests/'+rid+'/send').status==409)
            check('GET /models probe',request('post','/api/llm/probe').json()['ok'])
            page.locator('[data-llm="refresh"]').click();page.wait_for_timeout(350)
            page.evaluate("RoomLLM.setMode('legacy')");page.get_by_role('button',name='현재 설정으로 새 요청',exact=True).click()
            page.locator('.llm-output').wait_for(state='attached')
            check('real HTTP mock backend called exactly once',len(captured)==1)
            rid2=page.evaluate('RoomLLM.getSelection()');row=request('get','/api/llm/requests/'+rid2).json()
            check('captured exact request bytes',captured[0]==row['request_body'].encode('utf-8'))
            check('model API response preserved',row['response_json']['output']==out)
            check('tokens shown from provider',page.locator('.llm-metric').nth(1).locator('strong').inner_text().startswith('48'))
            check('decode TPS shown',page.locator('.llm-metric').nth(2).locator('strong').inner_text().startswith('24.00'))
            check('no model output HTML execution',not page.evaluate('Boolean(window.PWNED)'))
            check('no task auto-execution',len(request('get','/api/state').json()['tasks'])==0)
            # Editing config must survive poll and manager refresh.
            page.locator('[data-llm="settings"]').first.click();page.locator('textarea[name="system_prompt"]').fill('아직 저장하지 않은 입력 - 폴링 보존')
            page.wait_for_timeout(3400);page.evaluate('RoomManager.refresh()');page.wait_for_timeout(300)
            check('settings draft survives refresh',page.locator('textarea[name="system_prompt"]').input_value()=='아직 저장하지 않은 입력 - 폴링 보존')
            page.locator('#llmSettings [data-llm="settings"]').click()
            page.screenshot(path=str(artifacts/'screenshots/llm-live.png'),full_page=True)
            # Missing usage/time must remain unknown, not guessed.
            mode['usage']=False
            page.evaluate("RoomLLM.setMode('legacy')");page.get_by_role('button',name='현재 설정으로 새 요청',exact=True).click();page.wait_for_timeout(3500)
            check('missing usage remains dash',page.locator('.llm-metric').nth(1).locator('strong').inner_text().startswith('—'))
            check('missing generation timing remains dash',page.locator('.llm-metric').nth(2).locator('strong').inner_text().startswith('—'))
            check('history preserves all attempts',request('get','/api/llm/requests').json()['total']==3)
            # Voice hash reflects edits; unsaved text cannot be silently sent.
            page.wait_for_timeout(500);page.evaluate('RoomManager.refresh(false)');page.evaluate("RoomManager.navigate('voice')");page.wait_for_timeout(600);page.evaluate('RoomManager.refresh()');page.wait_for_timeout(200);check('linked LLM history button',page.get_by_role('button',name='LLM 기록 3개').count()==1)
            # Pairing/preview regression alongside new sidebar.
            page.evaluate("RoomManager.navigate('devices')");page.locator('#pairForm [name=baseUrl]').fill(base);page.locator('#pairForm button[type="submit"]').click();page.wait_for_selector('#pairLink');link=page.locator('#pairLink').input_value();page.evaluate('RoomManager.refresh()');page.wait_for_timeout(300);check('pair QR survives refresh',page.locator('#pairLink').input_value()==link)
            page.evaluate("RoomManager.navigate('overview')");page.wait_for_selector('#previewBox');w=page.locator('#previewBox').bounding_box()['width'];page.wait_for_timeout(1400);check('preview width stable',abs(page.locator('#previewBox').bounding_box()['width']-w)<1)
            page.evaluate("RoomManager.navigate('llm')");page.wait_for_selector('#llmList')
            for width,height in [(1194,834),(834,1194),(390,844)]:
                page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(200)
                check(f'no horizontal overflow {width}',page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2'))
            check('no browser JS errors',not result['page_errors'])
            # privacy ACLs with an actual paired display cookie.
            pair=request('post','/api/devices/pair',{'name':'test-ipad'}).json()
            display=browser.new_context();res=display.request.post(base+'/api/devices/claim',data={'code':pair['path'].split('=')[1]},headers={'X-Room-Request':'1'})
            check('paired client access retained',res.status==200 and display.request.get(base+'/api/state').status==200)
            check('LLM admin-only from actual display',display.request.get(base+'/api/llm/requests').status==401)
            browser.close()
            if browser_http:browser_http.close()
    finally:
        server.should_exit=True;th.join(10);backend.shutdown();backend.server_close();td.cleanup()
        result['passed']=len(result['checks']);(artifacts/'test-results/llm-browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':result['passed'],'page_errors':result['page_errors']},ensure_ascii=False))
if __name__=='__main__':main()
