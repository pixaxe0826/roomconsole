"""Real Uvicorn/SQLite via an explicit Python HTTP bridge; browser WS events injected.
Direct Chromium localhost navigation is blocked in this environment. LLM is a test double.
No user DB/model/microphone is used. Screenshots contain synthetic example data.
"""
import asyncio,json,os,re,shutil,socket,sys,tempfile,threading,time
from pathlib import Path
import httpx,uvicorn
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.main import create_app
from app.llm import sha

class Model:
    async def generate(self,endpoint,body,timeout):
        r={'choices':[{'message':{'content':'[모의 응답] 김치볶음밥 또는 카레를 추천합니다. 간단히 준비할 수 있습니다.'},'finish_reason':'stop'}],
           'model':'MOCK-ONLY','usage':{'prompt_tokens':60,'completion_tokens':30},'timings':{'predicted_n':30,'predicted_ms':1500,'predicted_per_second':20}}
        await asyncio.sleep(.1);return r,json.dumps(r,ensure_ascii=False)
    async def probe(self,cfg):return {'ok':True,'models':[cfg.model],'message':'test backend only'}

def load_bridged(page, api, kind):
    """Inline exact production CSS/JS. Test-only fetch/WS/URL loading adaptations."""
    def bridge(path, method, body):
        assert path.startswith('/api/'), path
        r=api.request(method,path,content=body,headers={'Content-Type':'application/json'} if body else None)
        return {'status':r.status_code,'body':r.text}
    if not getattr(page,'_room_bridge',False):
        page.expose_function('assistantHTTP',bridge);page._room_bridge=True
    html=(ROOT/f'web/{kind}.html').read_text()
    html=re.sub(r'<link\b[^>]*>','',html)
    html=re.sub(r'<script defer[^>]*></script>','',html)
    cssfiles=['base.css','client.css','speech.css'] if kind=='client' else ['base.css','manager.css','manager-llm.css']
    cssfiles.append('life.css')
    css='\n'.join((ROOT/'web'/x).read_text() for x in cssfiles)
    if kind=='client':css+='\n'+'\n'.join(x.read_text() for x in (ROOT/'widgets').glob('*/style.css'))
    code=(ROOT/'web/shared.js').read_text()+r"""
    window.fetch=(path,options={})=>new Promise((resolve,reject)=>{
      if(options.signal?.aborted){reject(new DOMException('Aborted','AbortError'));return;}
      options.signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
      window.assistantHTTP(String(path),options.method||'GET',options.body||null).then(r=>resolve(new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}}))).catch(reject);
    });
    history.replaceState=()=>{};
    Room.connect=(role,handler,status)=>{window.assistantInvalidate=()=>handler({type:'invalidate'});queueMicrotask(()=>{status(true);handler({type:'hello'})});const timer=setInterval(()=>handler({type:'invalidate'}),350);return {send:()=>{},close:()=>clearInterval(timer)};};
    """
    code+='\n'+(ROOT/'web/life.js').read_text()+'\n'
    if kind=='client':
        code+='window.ROOM_WIDGETS={};\n'
        for mod in (ROOT/'widgets').glob('*/widget.js'):
            text=mod.read_text().replace('export function','function');names='render,bind' if 'function bind(' in text else 'render'
            code+=f'window.ROOM_WIDGETS[{json.dumps(mod.parent.name)}]=(()=>{{{text}\nreturn {{{names}}};}})();\n'
        client=(ROOT/'web/client.js').read_text().replace('demo?window.ROOM_WIDGETS[m.id]:await import(`${m.baseUrl}/${m.entry}?v=${encodeURIComponent(m.version)}`)','window.ROOM_WIDGETS[m.id]')
        code+=client
    else:
        code+=(ROOT/'web/manager-llm.js').read_text()+'\n'+(ROOT/'web/manager.js').read_text().replace('src="/client"','src="about:blank"')
    html=html.replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+code.replace('</script','<\\/script')+'</script></body>')
    page.set_content(html,wait_until='domcontentloaded')


def main():
    results={'scope':'Real local Uvicorn HTTP/SQLite through Python fetch bridge; inlined production UI and injected browser invalidations; synthetic data/fake LLM; not direct browser networking or V35/iPad/Qwen.','checks':[],'page_errors':[]}
    def ok(name,value):
        assert value,name;results['checks'].append(name);print('PASS',name,flush=True)
    with tempfile.TemporaryDirectory() as td:
        app=create_app(Path(td)/'data',weather_enabled=False,llm_backend=Model())
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        th=threading.Thread(target=server.run,daemon=True);th.start();base=f'http://127.0.0.1:{port}'
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        with httpx.Client(base_url=base,trust_env=False,headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
            today=api.get('/api/state').json()['today']
            api.post('/api/tasks',json={'title':'책 읽기','date':today})
            api.post('/api/tasks',json={'title':'식물에 물 주기','date':today,'time':'18:30'})
            layout=api.get('/api/state').json()['layout'];layout['widgets'][0]['w']=2;layout['widgets'][1].update(x=2,w=2)
            layout['widgets'].append({'id':'llm','type':'llm-response','title':'비서 응답','x':4,'y':0,'w':4,'h':2,'config':{}})
            api.put('/api/layout',json=layout)
            texts=['오늘 할 일에 택배 보내기 추가해?','오늘 남은 할 일 확인해서 보고해줘.','저녁메뉴 추천해 줘.','오늘 할 일 전부 완료 처리해']
            voices=[]
            for i,t in enumerate(texts):voices.append(api.post('/api/voice/text',json={'request_id':f'browser-voice-{i}','source':'test-preview','text':t}).json()['id'])
            cfg=api.get('/api/llm/config').json()['config'];cfg['enabled']=True;api.put('/api/llm/config',json=cfg)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1100});page.on('pageerror',lambda e:results['page_errors'].append(str(e)))
                load_bridged(page,api,'manager');page.wait_for_selector('.kpis')
                page.evaluate("RoomManager.navigate('voice')");page.wait_for_selector('#voiceLLMMode')
                ok('default voice mode is auto',page.locator('#voiceLLMMode').input_value()=='auto')
                page.locator(f'[data-op=voice-to-llm][data-id="{voices[0]}"]').click()
                page.wait_for_selector('[data-agent-state=awaiting_confirmation]')
                ok('create with punctuation yields confirmation, not refusal','추가합니다' in page.locator('#llmDetail').inner_text())
                ok('no task inserted before confirmation',len(api.get('/api/state').json()['tasks'])==2)
                ok('normalized input and route visible','규칙 기반' in page.locator('#llmDetail').inner_text() and '택배 보내기 추가해?' in page.locator('.llm-transcript').inner_text())
                ok('token metrics not fabricated for rules','—' in page.locator('.llm-metrics').inner_text())
                screenshots=ROOT/'artifacts/screenshots';screenshots.mkdir(parents=True,exist_ok=True)
                page.evaluate("document.querySelector('#llmTopNotice').textContent='검증 예시 · 합성 데이터 · 실제 사용자 기록 또는 실제 Qwen 시험 아님'")
                page.wait_for_timeout(3300);page.screenshot(path=str(screenshots/'assistant_confirm.png'),full_page=True)
                # Second page fetches real read-only DTO via an explicit HTTP bridge.
                pair=api.post('/api/devices/pair',json={'name':'TEST iPad'}).json()
                tablet=browser.new_page(viewport={'width':1194,'height':834},has_touch=True)
                tablet.on('pageerror',lambda e:results['page_errors'].append(str(e)))
                display=httpx.Client(base_url=base,trust_env=False,headers={'X-Room-Request':'1'});assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
                load_bridged(tablet,display,'client');tablet.wait_for_selector('[data-llmr-output]')
                ok('paired display sees pending confirmation', '아직 변경하지 않았습니다' in tablet.locator('[data-llmr-output]').inner_text())
                page.locator('[data-llm=confirm-action]').click();page.wait_for_selector('[data-agent-state=succeeded]')
                ok('confirmation creates one real SQLite task',len(api.get('/api/state').json()['tasks'])==3)
                tablet.locator('[data-llmr-output]').filter(has_text='추가했습니다').wait_for()
                ok('injected invalidation fetches real confirmed result for tablet', '추가했습니다' in tablet.locator('[data-llmr-output]').inner_text())
                page.locator('[data-llm=retry]').click();page.locator('#llmDetail').filter(has_text='이미 실행한 작업').wait_for()
                ok('UI retry does not duplicate effect',len(api.get('/api/state').json()['tasks'])==3)
                page.evaluate("RoomManager.navigate('voice')");page.wait_for_selector(f'[data-id="{voices[1]}"][data-op=voice-to-llm]');page.locator(f'[data-id="{voices[1]}"][data-op=voice-to-llm]').click()
                page.wait_for_selector('[data-agent-state=succeeded]')
                ok('actual list includes task inserted by earlier request','택배 보내기' in page.locator('.llm-output').inner_text() and '3개' in page.locator('.llm-output').inner_text())
                page.screenshot(path=str(screenshots/'assistant_read.png'),full_page=True)
                page.evaluate("RoomManager.navigate('voice')");page.wait_for_selector('#voiceLLMMode')
                page.locator(f'[data-id="{voices[2]}"][data-op=voice-to-llm]').click();page.wait_for_selector('[data-agent-state=succeeded]')
                ok('general query routes to separate chat prompt','일반 대화' in page.locator('.assistant-panel').inner_text())
                ok('model output retained and labelled','[모의 응답]' in page.locator('.llm-output').inner_text())
                ok('actual provider fixture metrics shown','30' in page.locator('.llm-metrics').inner_text())
                page.screenshot(path=str(screenshots/'assistant_chat_mock.png'),full_page=True)
                page.evaluate("RoomManager.navigate('voice')");page.wait_for_selector('#voiceLLMMode');page.locator(f'[data-id="{voices[3]}"][data-op=voice-to-llm]').click();page.wait_for_selector('[data-agent-state=awaiting_confirmation]')
                ok('bulk target names and count displayed',page.locator('.assistant-targets li').count()==3)
                page.locator('.assistant-confirm-box [data-llm=cancel]').click();page.locator('.llm-detail-heading').filter(has_text='취소됨').wait_for()
                ok('cancelled bulk mutation changes nothing',all(not t['completed'] for t in api.get('/api/state').json()['tasks']))
                # Conflict after preview: no partial mutation.
                j=api.post('/api/llm/requests',json={'request_id':'browser-conflict-1','voice_id':voices[3],'expected_text_sha256':sha(texts[3]),'mode':'auto'}).json()
                for _ in range(100):
                    d=api.get('/api/llm/requests/'+j['id']).json()
                    if d['status']=='awaiting_confirmation':break
                    time.sleep(.01)
                tasks=api.get('/api/state').json()['tasks'];t=tasks[0]
                api.patch('/api/tasks/'+t['id'],json={'version':t['version'],'title':'변경한 제목'})
                res=api.post('/api/assistant/'+j['id']+'/confirm',json={'preview_sha256':d['assistant']['preview_sha256']})
                ok('live conflict refuses a stale bulk preview',res.status_code==409 and all(not t['completed'] for t in api.get('/api/state').json()['tasks']))
                ok('no inputs/confirm controls added to tablet',tablet.locator('input,textarea,select,[data-llm=confirm-action]').count()==0)
                tablet.screenshot(path=str(screenshots/'assistant_client.png'))
                # Ensure manager's original preview size fix remains.
                page.evaluate("RoomManager.navigate('overview')")
                # Query and measure in one browser turn: the live invalidation
                # handler can detach a previously resolved Locator element.
                measure = """() => {
                    const box = document.querySelector('#previewBox');
                    if (!box) return false;
                    const width = box.getBoundingClientRect().width;
                    return width > 0 ? width : false;
                }"""
                w1=page.wait_for_function(measure,timeout=5000).json_value()
                page.wait_for_timeout(1500)
                w2=page.wait_for_function(measure,timeout=5000).json_value()
                results['preview_widths']=[w1,w2]
                ok(f'preview width stable during live update ({w1}, {w2})',abs(w1-w2)<.2)
                ok('browser no uncaught errors',not results['page_errors'])
                browser.close();display.close()
        server.should_exit=True;th.join(timeout=10)
    results['passed']=len(results['checks']);out=ROOT/'artifacts/test-results/assistant-browser.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print('TOTAL',results['passed'])
if __name__=='__main__':main()
