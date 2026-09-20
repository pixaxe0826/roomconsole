"""Build standalone, network-free demos using the exact production HTML/CSS/JS."""
from pathlib import Path
import json,re
ROOT=Path(__file__).resolve().parents[1]

def build():
 from sys import path
 path.insert(0,str(ROOT))
 from app.store import DEFAULT_LAYOUT,DEFAULT_SETTINGS
 from app.widgets import discover
 registry,_=discover(ROOT/'widgets');today='2026-09-17';now='2026-09-17T00:41:00+00:00'
 raw=[
 ('2026-09-17','아침 스트레칭','07:30','personal',True,None),('2026-09-17','이번 주 일정 정리하기','10:00','work',False,None),('2026-09-17','책 20페이지 읽기','15:00','study',False,'series-reading'),('2026-09-17','식물에 물 주기','18:30','home',False,None),
 ('2026-09-01','새 달 계획 세우기','09:00','personal',True,None),('2026-09-03','디자인 미팅','14:00','work',True,None),('2026-09-05','주말 장보기',None,'home',True,None),('2026-09-07','운동하기','19:00','personal',True,None),('2026-09-09','독서 모임','20:00','study',True,None),('2026-09-11','프로젝트 점검','15:00','work',True,None),('2026-09-14','주간 계획','09:30','work',True,None),('2026-09-16','산책하기','18:00','personal',True,None),('2026-09-18','책 20페이지 읽기','15:00','study',False,'series-reading'),('2026-09-19','책 20페이지 읽기','15:00','study',False,'series-reading'),('2026-09-19','주말 장보기','11:00','home',False,None),('2026-09-20','방 정리하기','10:00','home',False,None),('2026-09-21','책 20페이지 읽기','15:00','study',False,'series-reading'),('2026-09-22','자료 정리','14:00','work',False,None),('2026-09-24','가족과 저녁','19:00','personal',False,None),('2026-09-26','식물 관리','10:00','home',False,None),('2026-09-28','다음 달 준비',None,'personal',False,None),('2026-09-30','월간 회고','20:00','work',False,None)]
 tasks=[{'id':f'task-{i}','title':title,'date':d,'time':t,'category':cat,'priority':'normal','notes':'','completed':done,'series_id':sid,'version':1,'created_at':now,'updated_at':now} for i,(d,title,t,cat,done,sid) in enumerate(raw)]
 demo={'revision':7,'server_time':now,'today':today,'settings':{**DEFAULT_SETTINGS,'location_name':'서울 · 예시','latitude':37.5665,'longitude':126.978},'layout':DEFAULT_LAYOUT,'tasks':tasks,'widgets':registry,'widget_errors':[],'widget_data':{},'weather':{'provider':'Open-Meteo','sample':True,'fetched_at':now,'location_name':'서울 · 예시','coordinates':[37.5665,126.978],'current':{'temperature_2m':24,'relative_humidity_2m':61,'apparent_temperature':24.8,'is_day':1,'precipitation':0,'weather_code':0,'wind_speed_10m':1.8},'daily':{'time':['2026-09-17','2026-09-18','2026-09-19','2026-09-20','2026-09-21','2026-09-22','2026-09-23'],'weather_code':[0,2,3,61,3,1,0],'temperature_2m_max':[27,27,25,24,25,26,27],'temperature_2m_min':[19,19,20,19,18,18,19],'precipitation_probability_max':[5,10,25,65,20,10,5]},'stale':False,'error':None},'weather_error':None,'capabilities':{'task_completion':True}}
 # Synthetic LLM projection fixture, never a real model response or benchmark.
 demo['capabilities']['llm_response_widget']=True
 demo['llm_display']={'enabled':True,'items':[],'total':3}
 demo['llm_display_demo_details']={}
 pairs=[
  ('내일 오후 세 시에 택배 보내기 추가해 줘.', '내일 오후 3시에 「택배 보내기」를 하려는 요청입니다.\n아직 할 일을 추가하지 않았습니다.'),
  ('이번 주에 공부할 내용을 정리해 줘.', '할 일을 날짜별로 나누어 확인하고, 중요한 과제부터 순서를 정해 보세요.\n이 내용은 화면 확인용 예시 답변입니다.'),
  ('오늘 할 일 전부 완료 처리해.', '오늘 할 일의 완료 처리를 요청하셨습니다.\n현재는 입력 확인 단계이며 실제 할 일은 변경하지 않았습니다.'),
 ]
 for i,(question,answer) in enumerate(pairs,1):
  rid=f'demo-llm-{i}';at=f'2026-09-17T00:{35+i:02d}:00+00:00'
  meta={'id':rid,'status':'succeeded','sent_at':at,'updated_at':at}
  demo['llm_display']['items'].append(meta)
  demo['llm_display_demo_details'][rid]={**meta,'finished_at':at,'model':'모의 응답 · 실제 LLM 아님','input':question,'output':answer,'refusal':None,'output_kind':'text','truncated':False}
 # Synthetic examples only. No alarms ring and no writes occur in file demos.
 demo['life_demo']={'notes':[
  {'id':'n1','title':'작은 것부터, 하나씩','body':'오늘의 생각을 남겨 두세요.\n\n책 20페이지 읽기\n주말 산책 코스 찾아보기','pinned':True,'shared':True,'version':1,'created_at':now,'updated_at':now},
  {'id':'n2','title':'다음에 읽을 책','body':'관심 있는 주제와 문장을 모아 두는 곳.','pinned':False,'shared':False,'version':1,'created_at':now,'updated_at':now}],
  'alarms':[{'id':'a1','label':'하루를 시작하는 시간','repeat':'weekly','weekdays':[0,1,2,3,4],'date':None,'time':'07:30','timezone':'Asia/Seoul','enabled':True,'next_fire_at':'2026-09-17T22:30:00+00:00','version':1},
  {'id':'a2','label':'잠깐, 몸을 움직여요','repeat':'weekly','weekdays':list(range(7)),'date':None,'time':'15:00','timezone':'Asia/Seoul','enabled':True,'next_fire_at':'2026-09-17T06:00:00+00:00','version':1}],
  'events':[]}
 demo['life']={'version':1,'revision':1,'as_of':now,'notes':{'enabled':True,'items':[demo['life_demo']['notes'][0]]},'alarms':{'enabled':True,'items':demo['life_demo']['alarms'],'events':[]},'scheduler':{'error':False,'delivery':'foreground_browser_only','sound_confirmed':False}}
 safe=lambda obj:json.dumps(obj,ensure_ascii=False).replace('<','\\u003c')
 common=(ROOT/'web/base.css').read_text(encoding='utf-8');shared=(ROOT/'web/shared.js').read_text(encoding='utf-8')
 def assemble(kind,prefix,extra=''):
  html=(ROOT/f'web/{kind}.html').read_text(encoding='utf-8');html=re.sub(r'<link\b[^>]*>','',html);html=re.sub(r'<script defer[^>]*></script>','',html)
  css=common+'\n'+(ROOT/'web/life.css').read_text('utf-8')+'\n'+(ROOT/f'web/{kind}.css').read_text(encoding='utf-8')+'\n'+(ROOT/'web/speech.css').read_text(encoding='utf-8')
  if kind=='manager':css+='\n'+(ROOT/'web/manager-llm.css').read_text('utf-8')
  if kind=='client':css+='\n'+'\n'.join((ROOT/'widgets'/m['id']/m['style']).read_text('utf-8') for m in registry)
  html=html.replace('</head>','<style>'+css+'</style></head>')
  llm=(ROOT/'scripts/llm_demo.js').read_text('utf-8')+'\n'+(ROOT/'web/manager-llm.js').read_text('utf-8') if kind=='manager' else ''
  scripts=prefix+'\n'+shared+'\n'+(ROOT/'web/life.js').read_text('utf-8')+'\n'+extra+'\n'+llm+'\n'+(ROOT/f'web/{kind}.js').read_text(encoding='utf-8')
  if kind=='client':scripts+='\n'+(ROOT/'web/speech.js').read_text(encoding='utf-8')
  return html.replace('</body>','<script>'+scripts.replace('</script','<\\/script')+'</script></body>')
 mod='window.ROOM_WIDGETS={};\n'
 for m in registry:
  src=(ROOT/'widgets'/m['id']/'widget.js').read_text(encoding='utf-8').replace('export function','function')
  names=['render']+(['bind'] if 'function bind(' in src else [])
  mod+=f'window.ROOM_WIDGETS[{json.dumps(m["id"])}]=(()=>{{{src}\nreturn {{{",".join(names)}}};}})();\n'
 client=assemble('client','window.ROOM_DEMO='+safe(demo)+';',mod)
 manager=assemble('manager','window.ROOM_DEMO='+safe(demo)+';\nwindow.ROOM_CLIENT_PREVIEW='+safe(client)+';')
 out=ROOT/'previews';out.mkdir(exist_ok=True)
 (out/'sample_state.json').write_text(json.dumps(demo,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
 (out/'client_preview.html').write_text(client,encoding='utf-8',newline='\n');(out/'manager_preview.html').write_text(manager,encoding='utf-8',newline='\n')
 # An explicitly labelled all-list demo. Production/default layouts remain unchanged.
 all_demo=json.loads(json.dumps(demo));all_demo['layout']['widgets'][2].update(type='all-todos',title='전체 할 일')
 all_client=assemble('client','window.ROOM_DEMO='+safe(all_demo)+';',mod)
 (out/'all_tasks_preview.html').write_text(all_client,encoding='utf-8',newline='\n')
 # Dedicated 4x2 widget preview; the production stored layout is unchanged.
 llm_demo=json.loads(json.dumps(demo))
 llm_demo['layout']['widgets'][0].update(w=2)
 llm_demo['layout']['widgets'][1].update(x=2,w=2)
 llm_demo['layout']['widgets'].append({'id':'llm','type':'llm-response','title':'LLM 응답','x':4,'y':0,'w':4,'h':2,'config':{}})
 llm_client=assemble('client','window.ROOM_DEMO='+safe(llm_demo)+';',mod)
 (out/'llm_widget_preview.html').write_text(llm_client,encoding='utf-8',newline='\n')
 life_demo=json.loads(json.dumps(demo))
 life_demo['layout']['widgets'][2].update(type='note',title='메모')
 life_demo['layout']['widgets'][3].update(type='alarms',title='알람')
 life_client=assemble('client','window.ROOM_DEMO='+safe(life_demo)+';',mod)
 (out/'notes_alarms_preview.html').write_text(life_client,encoding='utf-8',newline='\n')
 print('Built',len(client.encode()),len(manager.encode()),len(all_client.encode()),'bytes')
if __name__=='__main__':build()
