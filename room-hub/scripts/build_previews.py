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
 safe=lambda obj:json.dumps(obj,ensure_ascii=False).replace('<','\\u003c')
 common=(ROOT/'web/base.css').read_text(encoding='utf-8');shared=(ROOT/'web/shared.js').read_text(encoding='utf-8')
 def assemble(kind,prefix,extra=''):
  html=(ROOT/f'web/{kind}.html').read_text(encoding='utf-8');html=re.sub(r'<link\b[^>]*>','',html);html=re.sub(r'<script defer[^>]*></script>','',html)
  css=common+'\n'+(ROOT/f'web/{kind}.css').read_text(encoding='utf-8')
  if kind=='client':css+='\n'+'\n'.join((ROOT/'widgets'/m['id']/m['style']).read_text('utf-8') for m in registry)
  html=html.replace('</head>','<style>'+css+'</style></head>')
  scripts=prefix+'\n'+shared+'\n'+extra+'\n'+(ROOT/f'web/{kind}.js').read_text(encoding='utf-8')
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
 print('Built',len(client.encode()),len(manager.encode()),'bytes')
if __name__=='__main__':build()
