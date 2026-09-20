"""0.1.6 UI + real local FastAPI/SQLite via explicit Python HTTP bridge.
Chromium network and WS notifications are adapted by the existing test harness.
Responses are synthetic; no V35, Safari, microphone or Qwen inference tested.
"""
import asyncio,json,os,shutil,socket,sys,tempfile,threading,time
from pathlib import Path
import httpx,uvicorn
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from assistant_browser import load_bridged
from app.main import create_app
from app.llm import sha

class Model:
 def __init__(self):self.calls=[]
 async def generate(self,endpoint,body,timeout):
  self.calls.append(json.loads(body));text='일어나 '*80
  r={'choices':[{'message':{'content':text},'finish_reason':'length'}],
     'model':'SYNTHETIC-ONLY','usage':{'prompt_tokens':48,'completion_tokens':128},
     'timings':{'predicted_n':128,'predicted_ms':1000,'predicted_per_second':128}}
  return r,json.dumps(r,ensure_ascii=False)
 async def probe(self,cfg):return {'ok':True,'models':[cfg.model],'message':'test model only'}

def main():
 out={'scope':__doc__,'checks':[],'page_errors':[]}
 def check(name,yes):
  assert yes,name;out['checks'].append(name);print('PASS',name,flush=True)
 with tempfile.TemporaryDirectory() as td:
  model=Model();app=create_app(Path(td)/'data',weather_enabled=False,llm_backend=model)
  sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
  server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
  th=threading.Thread(target=server.run,daemon=True);th.start()
  for _ in range(100):
   if server.started:break
   time.sleep(.05)
  url=f'http://127.0.0.1:{port}'
  try:
   with httpx.Client(base_url=url,trust_env=False,headers={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}) as api:
    clock=api.get('/api/clock').json();today,tomorrow=clock['today'],clock['tomorrow']
    ids=[]
    for title,day,tm in [('검증 예시: 미완료 항목',today,None),('검증 예시: 완료 항목',today,None),('검증 예시: 내일 택배',tomorrow,'14:00')]:
     ids+=api.post('/api/tasks',json={'title':title,'date':day,'time':tm}).json()['ids']
    api.patch('/api/tasks/'+ids[1]+'/completion',json={'version':1,'completed':True})
    cfg=api.get('/api/llm/config').json()['config'];cfg['enabled']=True;api.put('/api/llm/config',json=cfg)
    layout=api.get('/api/state').json()['layout'];layout['widgets'][0]['w']=2;layout['widgets'][1].update(x=2,w=2)
    layout['widgets'].append({'id':'llm','type':'llm-response','title':'서버 응답','x':4,'y':0,'w':4,'h':2,'config':{}});api.put('/api/layout',json=layout)
    texts=['오늘, 남은 할 일 확인해','내일 뭐 해야 돼?','좋은 습관 추천해','내일 할 일에 확인 테스트 추가해']
    vids=[]
    for i,text in enumerate(texts):vids.append(api.post('/api/voice/text',json={'request_id':f'context-voice-{i}','source':'SYNTHETIC','text':text}).json()['id'])
    with sync_playwright() as p:
     browser=p.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
     page=browser.new_page(viewport={'width':1440,'height':1050});page.on('pageerror',lambda e:out['page_errors'].append(str(e)))
     load_bridged(page,api,'manager');page.wait_for_selector('.kpis')
     def send(i,state):
      page.evaluate("RoomManager.navigate('voice')");page.wait_for_selector('#voiceLLMMode')
      page.locator(f'[data-op=voice-to-llm][data-id="{vids[i]}"]').click()
      page.wait_for_selector('[data-agent-state='+state+']')
     send(0,'succeeded')
     check('boundary comma uses rule, not model','규칙 기반' in page.locator('.assistant-panel').inner_text() and not model.calls)
     check('pending filter excludes completed row','완료 항목' not in page.locator('.llm-output').inner_text().replace('미완료 항목',''))
     check('DB provenance, count, source date shown','room_hub_sqlite' in page.locator('.assistant-panel').inner_text() and '한국 기준 시각' in page.locator('.assistant-panel').inner_text())
     page.locator('details[data-section=capabilities]').evaluate('(e)=>e.open=true')
     page.locator('#assistantCatalog').filter(has_text='todo.list').wait_for()
     check('capability list and unsupported future features visible','알람' in page.locator('#assistantCatalog').inner_text())
     check('live server time populated',clock['today'] in page.locator('#assistantClock').inner_text())
     check('no fake model statistics','—' in page.locator('.llm-metrics').inner_text())
     shots=ROOT/'artifacts/screenshots';shots.mkdir(exist_ok=True,parents=True)
     page.wait_for_timeout(3200)
     page.screenshot(path=str(shots/'context_pending.png'),full_page=True)
     send(1,'succeeded')
     check('tomorrow colloquial phrase yields actual tasks','내일 택배' in page.locator('.llm-output').inner_text() and tomorrow in page.locator('.llm-output').inner_text())
     check('tomorrow query no model',not model.calls)
     pair=api.post('/api/devices/pair',json={'name':'TEST tablet'}).json()
     with httpx.Client(base_url=url,trust_env=False,headers={'X-Room-Request':'1'}) as display:
      display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]})
      tablet=browser.new_page(viewport={'width':1194,'height':834},has_touch=True)
      tablet.on('pageerror',lambda e:out['page_errors'].append(str(e)))
      load_bridged(tablet,display,'client');tablet.wait_for_selector('[data-llmr-output]')
      tablet.locator('[data-llmr-output]').filter(has_text='내일 택배').wait_for()
      check('client widget receives actual DB-based result','내일 택배' in tablet.locator('[data-llmr-output]').inner_text())
      check('display cannot read private capability/task endpoints',display.get('/api/assistant/capabilities').status_code==401 and display.get('/api/assistant/tasks',params={'start':today,'end':today}).status_code==401)
      send(2,'needs_clarification')
      check('repeat is not succeeded',page.locator('[data-quality-rejected=true]').count()==1)
      check('repeat never shown as final answer','일어나 일어나' not in page.locator('.llm-output').inner_text())
      page.locator('details[data-section=agent-calls]').evaluate('(e)=>e.open=true')
      check('exact rejected output retained for manager','일어나 일어나' in page.locator('details[data-section=agent-calls]').inner_text())
      check('actual invocation sampling + cap',model.calls[-1]['max_tokens']==128 and model.calls[-1]['temperature']==.7)
      tablet.locator('[data-llmr-output]').filter(has_text='답변을 채택하지').wait_for()
      check('display sees safe quality message','일어나 일어나' not in tablet.locator('[data-llmr-output]').inner_text())
      page.locator('details[data-section=agent-calls]').evaluate('(e)=>e.open=false')
      page.wait_for_timeout(3200)
      page.screenshot(path=str(shots/'context_quality.png'),full_page=True)
      send(3,'awaiting_confirmation')
      check('create still awaits manager confirmation',len(api.get('/api/state').json()['tasks'])==3)
      page.locator('[data-llm=confirm-action]').click();page.wait_for_selector('[data-agent-state=succeeded]')
      check('approved task stored',len(api.get('/api/state').json()['tasks'])==4)
      check('no edit UI added to iPad',tablet.locator('input,textarea,select,[data-llm=confirm-action]').count()==0)
      page.evaluate("RoomManager.navigate('overview')");page.wait_for_selector('#previewBox')
      w=page.locator('#previewBox').bounding_box()['width'];page.wait_for_timeout(1500)
      check('preview stable',abs(w-page.locator('#previewBox').bounding_box()['width'])<.2)
      check('no uncaught browser exceptions',not out['page_errors'])
     browser.close()
  finally:server.should_exit=True;th.join(timeout=10)
 out['passed']=len(out['checks']);dest=ROOT/'artifacts/test-results/context-browser.json';dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(json.dumps(out,ensure_ascii=False,indent=2));print('TOTAL',out['passed'])
if __name__=='__main__':main()
