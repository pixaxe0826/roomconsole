import asyncio,contextlib,hashlib,hmac,io,json,os,secrets,time,sqlite3,re
from contextlib import asynccontextmanager
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import qrcode,qrcode.image.svg
from fastapi import FastAPI,Request,Response,HTTPException,Depends,UploadFile,File,Form,WebSocket,WebSocketDisconnect
from fastapi.responses import FileResponse,JSONResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from .models import *
from . import llm_display
from .life import LifeService, register_routes as register_life_routes
from .clock_service import clock_context
from .capabilities import manifest as capability_manifest
from .assistant import Confirmation
from .store import Store,uid,utcnow
from .recurrence import occurrences
from .widgets import discover
from .weather import fetch_weather
from .speech import SpeechHub
from .llm import LLMHub, LLMConfig, LLMCreate, LLMRetry, sha as llm_sha
ROOT=Path(__file__).resolve().parent.parent

def hashed(value):return hashlib.sha256(value.encode()).hexdigest()

def create_app(data_dir=None,weather_enabled=True,*,speech_config=None,speech_runner=None,llm_backend=None):
 data=Path(data_dir or os.getenv('HUB_DATA_DIR',ROOT/'data')).resolve();data.mkdir(parents=True,exist_ok=True);(data/'audio').mkdir(exist_ok=True)
 widget_root=Path(os.getenv('HUB_WIDGET_DIR',ROOT/'widgets')).resolve()
 store=Store(data/'room-hub.sqlite3');connections={};attempts={};wx={'last_attempt':0.,'error':None};wx_lock=asyncio.Lock()
 secure=os.getenv('HUB_SECURE_COOKIE','false').lower()=='true'
 def secret(env,name):
  v=os.getenv(env);path=data/name
  if not v:
   if path.exists():v=path.read_text().strip()
   else:v=secrets.token_urlsafe(32);path.write_text(v+'\n');path.chmod(0o600)
  if len(v)<24:raise RuntimeError(env+' must contain at least 24 characters')
  return v
 admin_key=secret('HUB_ADMIN_TOKEN','admin-token.txt');ingest_key=secret('HUB_INGEST_TOKEN','ingest-token.txt')
 async def send(ws,payload):
  try:await asyncio.wait_for(ws.send_json(payload),3);return True
  except Exception:return False
 async def broadcast(payload,role=None,device_id=None):
  targets=[ws for ws,m in list(connections.items()) if (not role or m['role']==role) and (not device_id or m.get('device_id')==device_id)]
  results=await asyncio.gather(*(send(ws,payload) for ws in targets))
  for ws,ok in zip(targets,results):
   if not ok:connections.pop(ws,None)
  return sum(results)
 async def changed(event,detail=''):
  store.bump(event,detail);await broadcast({'type':'invalidate','revision':store.get('revision')})
 async def refresh_weather():
  async with wx_lock:
   settings=store.get('settings');wx['last_attempt']=time.monotonic()
   if settings['latitude'] is None:store.set('weather',None);wx['error']=None;await changed('weather.unconfigured');return
   try:
    result=await fetch_weather(settings);latest=store.get('settings')
    if any(latest[k]!=settings[k] for k in ['latitude','longitude','timezone','location_name']):return
    store.set('weather',result);wx['error']=None
   except Exception:wx['error']='날씨 공급자에 연결하지 못했습니다. 마지막 데이터를 표시합니다.'
   await changed('weather.refreshed')
 async def weather_loop():
  while True:
   settings=store.get('settings')
   if settings['latitude'] is not None and time.monotonic()-wx['last_attempt']>=settings['weather_interval_minutes']*60:await refresh_weather()
   await asyncio.sleep(20)
 speech=SpeechHub(store,data,changed,config=speech_config,runner=speech_runner)
 llm=LLMHub(store,changed,speech=speech,backend=llm_backend)
 life=LifeService(store,changed)
 @asynccontextmanager
 async def lifespan(app):
  await speech.start()
  await llm.start()
  task=asyncio.create_task(weather_loop()) if weather_enabled else None
  alarm_task=asyncio.create_task(life.run(),name="room-hub-alarms")
  try:
   yield
  finally:
   alarm_task.cancel()
   with contextlib.suppress(asyncio.CancelledError):await alarm_task
  await llm.close()
  await speech.close()
  if task:
   task.cancel()
   with contextlib.suppress(asyncio.CancelledError):await task
  for ws in list(connections):
   with contextlib.suppress(Exception):await ws.close()
 app=FastAPI(title='Room Hub',version='0.1.7',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
 app.state.life=life
 app.state.speech=speech
 app.state.llm=llm
 app.state.store=store;app.state.admin_token=admin_key;app.state.ingest_token=ingest_key;app.state.connections=connections
 @app.middleware('http')
 async def security(request,call_next):
  if request.method in {'POST','PUT','PATCH','DELETE'}:
   origin=request.headers.get('origin')
   if origin and urlparse(origin).netloc!=request.headers.get('host'):return JSONResponse({'detail':'다른 출처의 요청은 허용하지 않습니다.'},403)
   if request.cookies and request.headers.get('x-room-request')!='1' and not request.headers.get('authorization'):return JSONResponse({'detail':'요청 확인 헤더가 필요합니다.'},403)
   try:
    if int(request.headers.get('content-length','0'))>11*1024*1024:return JSONResponse({'detail':'파일은 최대 10MB입니다.'},413)
   except ValueError:return JSONResponse({'detail':'잘못된 요청 길이'},400)
   pieces=[];size=0
   async for piece in request.stream():
    size+=len(piece)
    if size>11*1024*1024:return JSONResponse({'detail':'파일은 최대 10MB입니다.'},413)
    pieces.append(piece)
   request._body=b''.join(pieces)
  authority=request.headers.get('host','')
  if not re.fullmatch(r'[A-Za-z0-9.\-\[\]:]+',authority):return JSONResponse({'detail':'잘못된 Host 헤더'},400)
  response=await call_next(request)
  response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'SAMEORIGIN',
   'Permissions-Policy':'microphone=(self), camera=(), geolocation=()',
   'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ws://"+authority+" wss://"+authority+"; frame-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"})
  if request.url.path.startswith('/api/') or request.url.path in ['/','/client','/manager']:response.headers['Cache-Control']='no-store'
  if request.url.path.startswith(('/static/','/widgets/')):response.headers['Cache-Control']='no-cache'
  return response
 def read_session(token):
  if not token:return None
  with store.connect() as db:
   row=db.execute('SELECT * FROM sessions WHERE hash=? AND expires_at>?',(hashed(token),time.time())).fetchone()
   if not row:return None
   if row['device_id']:
    dev=db.execute('SELECT revoked FROM devices WHERE id=?',(row['device_id'],)).fetchone()
    if not dev or dev['revoked']:return None
   return dict(row)
 def authenticate(request,role='viewer'):
  auth=request.headers.get('authorization','')
  if auth.startswith('Bearer ') and hmac.compare_digest(auth[7:],admin_key):return {'role':'admin','device_id':None}
  session=read_session(request.cookies.get('room_admin'))
  if session and session['role']=='admin':return session
  session=read_session(request.cookies.get('room_display'))
  if role=='viewer' and session:return session
  raise HTTPException(401,'관리자 로그인 또는 표시 기기 연결이 필요합니다.')
 def admin(request:Request):return authenticate(request,'admin')
 def viewer(request:Request):return authenticate(request)
 def intake(request:Request):
  auth=request.headers.get('authorization','')
  if auth.startswith('Bearer ') and hmac.compare_digest(auth[7:],ingest_key):return {'role':'ingest'}
  return authenticate(request,'admin')
 register_life_routes(app,life,admin,viewer,changed)
 def new_session(role,device_id,days):
  token=secrets.token_urlsafe(32)
  with store.connect() as db:
   db.execute('DELETE FROM sessions WHERE expires_at<?',(time.time(),));db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashed(token),role,device_id,time.time()+days*86400))
  return token
 @app.get('/healthz')
 async def health():return {'status':'ok','version':'0.1.7'}
 @app.get('/')
 async def root():return RedirectResponse('/client')
 @app.get('/client')
 async def client_page():return FileResponse(ROOT/'web/client.html')
 @app.get('/manager')
 async def manager_page():return FileResponse(ROOT/'web/manager.html')
 @app.post('/api/auth/login')
 async def login(body:Login,request:Request,response:Response):
  ip=request.client.host if request.client else 'unknown';recent=[x for x in attempts.get(ip,[]) if x>time.time()-300];attempts[ip]=recent
  if len(recent)>=10:raise HTTPException(429,'잠시 후 다시 시도하세요.')
  if not hmac.compare_digest(body.token,admin_key):recent.append(time.time());raise HTTPException(401,'관리자 키를 확인하세요.')
  attempts.pop(ip,None);response.set_cookie('room_admin',new_session('admin',None,.5),max_age=43200,httponly=True,secure=secure,samesite='strict');return {'ok':True}
 @app.post('/api/auth/logout')
 async def logout(request:Request,response:Response,_=Depends(admin)):
  with store.connect() as db:db.execute('DELETE FROM sessions WHERE hash=?',(hashed(request.cookies.get('room_admin','')),))
  response.delete_cookie('room_admin');return {'ok':True}
 @app.get('/api/auth/me')
 async def me(session=Depends(viewer)):return {'role':session['role'],'device_id':session.get('device_id')}
 @app.get('/api/state')
 async def state(_=Depends(viewer)):
  settings=store.get('settings');registry,errors=discover(widget_root);weather=store.get('weather')
  if weather:
   weather['stale']=(datetime.now(timezone.utc)-datetime.fromisoformat(weather['fetched_at'])).total_seconds()>settings['weather_interval_minutes']*120;weather['error']=wx['error']
  stamp=utcnow();clock=clock_context(stamp,settings['timezone'])
  return {'revision':store.get('revision'),'server_time':stamp,'today':clock['today'],'clock':clock,
   'settings':settings,'layout':store.get('layout'),'tasks':store.tasks(),'weather':weather,'weather_error':wx['error'],'widgets':registry,'widget_errors':errors,
   'capabilities':{'task_completion':True,'speech_upload':True,'llm_response_widget':True},
   'llm_display':llm_display.index(store),'life':life.display(),
   'widget_data':{w['id']:store.get('widget_data:'+w['id']) or {} for w in registry}}
 @app.get('/api/clock')
 async def current_clock(_=Depends(viewer)):
  return clock_context(utcnow(),store.get('settings')['timezone'])
 @app.get('/api/assistant/capabilities')
 async def assistant_capabilities(_=Depends(admin)):
  return capability_manifest()
 @app.get('/api/assistant/tasks')
 async def assistant_tasks(start:str,end:str,status:str='all',limit:int=50,_=Depends(admin)):
  try:return store.query_tasks(start,end,status,limit)
  except ValueError as exc:raise HTTPException(422,str(exc))
 @app.get('/api/display/llm/{rid}')
 async def llm_display_entry(rid:str,_=Depends(viewer)):
  if not rid or len(rid)>80:raise HTTPException(422,'기록 ID를 확인하세요.')
  return llm_display.entry(store,rid)
 @app.get('/api/admin/overview')
 async def overview(_=Depends(admin)):
  with store.connect() as db:
   devices=[dict(r) for r in db.execute('SELECT * FROM devices ORDER BY created_at DESC')];events=[dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 30')]
   voice=[dict(r) for r in db.execute('SELECT v.id,v.request_id,v.source,v.kind,v.text,v.locale,v.status,v.created_at,j.id AS job_id,j.status AS job_status,j.error AS transcription_error,j.elapsed AS transcription_elapsed,(SELECT count(*) FROM llm_requests l WHERE l.source_voice_id=v.id) AS llm_count FROM voice v LEFT JOIN speech_jobs j ON j.voice_id=v.id ORDER BY v.created_at DESC LIMIT 100')]
  for v in voice:v['text_sha256']=llm_sha(v['text'])
  for dev in devices:
   active=[m for m in connections.values() if m.get('device_id')==dev['id']];dev['online']=bool(active);dev['view']=active[-1].get('view') if active else None;dev['last_command']=active[-1].get('last_command') if active else None
  registry,errors=discover(widget_root)
  return {'devices':devices,'events':events,'voice':voice,'speech':speech.status(),'widgets':registry,'widget_errors':errors,'stats':{'connected':sum(d['online'] for d in devices),'tasks':len(store.tasks())}}
 @app.post('/api/devices/pair')
 async def pair(body:PairCreate,_=Depends(admin)):
  did=uid();code=secrets.token_urlsafe(24)
  with store.connect() as db:
   db.execute('INSERT INTO devices(id,name,created_at) VALUES(?,?,?)',(did,body.name,utcnow()));db.execute('INSERT INTO pairs VALUES(?,?,?,0)',(hashed(code),did,time.time()+600))
  await changed('device.pair_created',body.name);return {'device_id':did,'path':'/client#pair='+code,'expires_in':600}
 @app.post('/api/devices/claim')
 async def claim(body:PairAccept,response:Response):
  with store.connect() as db:
   db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT p.* FROM pairs p JOIN devices d ON p.device_id=d.id WHERE p.hash=? AND p.used=0 AND p.expires_at>? AND d.revoked=0',(hashed(body.code),time.time())).fetchone()
   if not row:raise HTTPException(401,'연결 링크가 만료되었거나 이미 사용되었습니다. 새 링크가 필요합니다.')
   db.execute('UPDATE pairs SET used=1 WHERE hash=?',(hashed(body.code),))
  response.set_cookie('room_display',new_session('display',row['device_id'],30),max_age=2592000,httponly=True,secure=secure,samesite='strict');await changed('device.paired',row['device_id']);return {'ok':True,'device_id':row['device_id']}
 @app.delete('/api/devices/{did}')
 async def revoke(did:str,_=Depends(admin)):
  with store.connect() as db:
   if not db.execute('UPDATE devices SET revoked=1 WHERE id=?',(did,)).rowcount:raise HTTPException(404,'기기가 없습니다.')
   db.execute('DELETE FROM sessions WHERE device_id=?',(did,));db.execute('DELETE FROM pairs WHERE device_id=?',(did,))
  for ws,m in list(connections.items()):
   if m.get('device_id')==did:
    await send(ws,{'type':'revoked'})
    with contextlib.suppress(Exception):await ws.close(code=4001)
    connections.pop(ws,None)
  await changed('device.revoked',did);return {'ok':True}
 @app.get('/api/admin/qr')
 async def qr(url:str,_=Depends(admin)):
  if len(url)>2000 or urlparse(url).scheme not in ['http','https']:raise HTTPException(422,'잘못된 주소')
  image=qrcode.make(url,image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=2);out=io.BytesIO();image.save(out);return Response(out.getvalue(),media_type='image/svg+xml')
 @app.post('/api/commands')
 async def command(body:RemoteCommand,_=Depends(admin)):
  if body.action=='expand' and not any(w['id']==body.widget_id for w in store.get('layout')['widgets']):raise HTTPException(422,'해당 위젯이 없습니다.')
  if body.action=='select_date' and not body.date:raise HTTPException(422,'날짜가 필요합니다.')
  payload={'type':'command','id':uid(),'sent_at':utcnow(),'expires_at':time.time()+30,**body.model_dump(mode='json')}
  count=await broadcast(payload,role='display',device_id=body.device_id);return {'id':payload['id'],'delivered_connections':count}
 @app.put('/api/layout')
 async def layout(body:Layout,_=Depends(admin)):
  current=store.get('layout')
  if current['version']!=body.version:raise HTTPException(409,'배치가 변경되었습니다. 다시 불러오세요.')
  registry,errors=discover(widget_root)
  if any(w.type not in {m['id'] for m in registry} for w in body.widgets):raise HTTPException(422,'설치되지 않은 위젯입니다.')
  result=body.model_dump();result['version']+=1;store.set('layout',result);await changed('layout.updated');return result
 @app.put('/api/settings')
 async def settings(body:HubSettings,_=Depends(admin)):
  old=store.get('settings');new=body.model_dump();store.set('settings',new)
  if any(old[k]!=new[k] for k in ['latitude','longitude','location_name','timezone']):store.set('weather',None);wx.update(last_attempt=0,error=None)
  await changed('settings.updated');return new
 @app.post('/api/weather/refresh')
 async def refresh(_=Depends(admin)):
  if weather_enabled:await refresh_weather()
  return {'weather':store.get('weather'),'error':wx['error']}
 @app.post('/api/tasks/preview')
 async def preview(body:TaskCreate,_=Depends(admin)):
  try:dates=occurrences(body)
  except ValueError as e:raise HTTPException(422,str(e))
  return {'count':len(dates),'dates':[d.isoformat() for d in dates]}
 @app.post('/api/tasks',status_code=201)
 async def add_task(body:TaskCreate,_=Depends(admin)):
  try:dates=occurrences(body)
  except ValueError as e:raise HTTPException(422,str(e))
  sid=uid() if body.repeat.frequency!='none' else None;now=utcnow();ids=[]
  with store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   if db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]+len(dates)>10000:raise HTTPException(422,'전체 작업은 최대 10,000개입니다.')
   if sid:db.execute('INSERT INTO series VALUES(?,?,?)',(sid,body.model_dump_json(),now))
   for d in dates:
    tid=uid();ids.append(tid);db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,0,?,1,?,?)',(tid,body.title,d.isoformat(),body.time,body.category,body.priority,body.notes,sid,now,now))
  await changed('task.created',f'{body.title} · {len(ids)}개');return {'ids':ids,'count':len(ids),'series_id':sid}
 @app.patch('/api/tasks/{tid}')
 async def edit_task(tid:str,body:TaskPatch,_=Depends(admin)):
  fields=body.model_dump(exclude_unset=True,mode='json');version=fields.pop('version')
  if not fields:raise HTTPException(422,'변경할 값이 없습니다.')
  if any(k!='time' and v is None for k,v in fields.items()):raise HTTPException(422,'필수 필드는 비울 수 없습니다.')
  fields['updated_at']=utcnow()
  with store.connect() as db:
   updated=db.execute('UPDATE tasks SET '+','.join(k+'=?' for k in fields)+',version=version+1 WHERE id=? AND version=?',(*fields.values(),tid,version))
   if not updated.rowcount:
    exists=db.execute('SELECT id FROM tasks WHERE id=?',(tid,)).fetchone();raise HTTPException(409 if exists else 404,'작업이 변경되었거나 삭제되었습니다.')
   task=store.task_dict(db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
  await changed('task.updated',tid);return task
 @app.patch('/api/tasks/{tid}/completion')
 async def complete_task(tid:str,body:TaskCompletion,session=Depends(viewer)):
  # Use an explicit desired value, not a toggle. Serialize the read/check/write
  # so two screens cannot both overwrite the same version of an occurrence.
  modified=False
  with store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   row=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
   if not row:raise HTTPException(404,'작업이 삭제되었거나 존재하지 않습니다.')
   if row['version']!=body.version:
    raise HTTPException(409,'다른 화면에서 작업이 변경되었습니다. 최신 상태를 확인해 주세요.')
   if bool(row['completed'])!=body.completed:
    db.execute('UPDATE tasks SET completed=?,version=version+1,updated_at=? WHERE id=? AND version=?',
     (int(body.completed),utcnow(),tid,body.version));modified=True
   task=store.task_dict(db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
  if modified:
   actor=session.get('device_id') or 'admin'
   await changed('task.completion',f'{tid} · {"완료" if body.completed else "완료 취소"} · {actor}')
  return task
 @app.delete('/api/tasks/{tid}')
 async def delete_task(tid:str,scope:str='one',version:int|None=None,_=Depends(admin)):
  if scope not in ['one','future','series']:raise HTTPException(422,'잘못된 삭제 범위')
  with store.connect() as db:
   db.execute('BEGIN IMMEDIATE');task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
   if not task:raise HTTPException(404,'작업이 없습니다.')
   if version is not None and task['version']!=version:raise HTTPException(409,'작업이 변경되었습니다.')
   if scope=='one' or not task['series_id']:cur=db.execute('DELETE FROM tasks WHERE id=?',(tid,))
   elif scope=='future':cur=db.execute('DELETE FROM tasks WHERE series_id=? AND date>=?',(task['series_id'],task['date']))
   else:cur=db.execute('DELETE FROM tasks WHERE series_id=?',(task['series_id'],))
   count=cur.rowcount;db.execute('DELETE FROM series WHERE id NOT IN(SELECT series_id FROM tasks WHERE series_id IS NOT NULL)')
  await changed('task.deleted',str(count));return {'deleted':count}
 @app.get('/api/admin/export')
 async def export(_=Depends(admin)):
  with store.connect() as db:series=[dict(r) for r in db.execute('SELECT * FROM series')]
  payload={'schema_version':1,'exported_at':utcnow(),'settings':store.get('settings'),'layout':store.get('layout'),'tasks':store.tasks(),'series':series,
   'life':{'schema_version':1,'notes':life.notes(),'alarms':life.alarms(),'alarm_events':life.events()}}
  return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="room-hub-export.json"'})
 @app.get('/api/admin/backup')
 async def backup(_=Depends(admin)):
  path=data/'room-hub-backup.sqlite3'
  with store.connect() as src,sqlite3.connect(path) as dest:src.backup(dest)
  return FileResponse(path,media_type='application/octet-stream',filename=path.name)
 @app.post('/api/widgets/reload')
 async def reload_widgets(_=Depends(admin)):
  registry,errors=discover(widget_root);await changed('widgets.reloaded');return {'widgets':registry,'errors':errors}
 @app.put('/api/widgets/{kind}/data')
 async def set_widget_data(kind:str,body:WidgetData,_=Depends(admin)):
  registry,_e=discover(widget_root)
  if kind not in {w['id'] for w in registry}:raise HTTPException(404,'위젯이 없습니다.')
  if len(json.dumps(body.data).encode())>100000:raise HTTPException(413,'위젯 데이터는 최대 100KB입니다.')
  store.set('widget_data:'+kind,body.data);await changed('widget.data',kind);return {'ok':True}
 @app.get('/api/widgets/{kind}/data')
 async def widget_data(kind:str,_=Depends(viewer)):return {'data':store.get('widget_data:'+kind) or {}}
 def insert_voice(values):
  with store.connect() as db:
   row=db.execute('SELECT id,status,digest FROM voice WHERE source=? AND request_id=?',(values['source'],values['request_id'])).fetchone()
   if row:
    if not hmac.compare_digest(row['digest'],values['digest']):raise HTTPException(409,'같은 요청 ID에 다른 내용이 들어왔습니다.')
    return {'id':row['id'],'status':row['status'],'duplicate':True}
   db.execute('INSERT INTO voice(id,request_id,source,kind,text,locale,status,metadata,filename,media_type,digest,created_at) VALUES(:id,:request_id,:source,:kind,:text,:locale,:status,:metadata,:filename,:media_type,:digest,:created_at)',values)
  return {'id':values['id'],'status':values['status'],'duplicate':False}
 @app.post('/api/voice/text',status_code=202)
 async def voice_text(body:VoiceText,_=Depends(intake)):
  values={'id':uid(),'request_id':body.request_id,'source':body.source,'kind':'text','text':body.text,'locale':body.locale,'status':'pending_review','metadata':json.dumps(body.metadata,ensure_ascii=False),'filename':None,'media_type':'text/plain','digest':hashed(json.dumps(body.model_dump(mode='json'),sort_keys=True,ensure_ascii=False)),'created_at':utcnow()}
  result=insert_voice(values)
  if not result['duplicate']:await changed('voice.received','text')
  return result
 @app.post('/api/voice/upload',status_code=202)
 async def voice_upload(file:UploadFile=File(...),request_id:str=Form(...),source:str=Form('manual'),locale:str=Form('ko-KR'),_=Depends(intake)):
  try:validated=VoiceText(request_id=request_id,source=source,locale=locale,text='validate')
  except Exception:raise HTTPException(422,'요청 ID/source/locale을 확인하세요.')
  blob=await file.read(10*1024*1024+1)
  if not blob or len(blob)>10*1024*1024:raise HTTPException(413,'파일 크기는 1바이트~10MB입니다.')
  mime=(file.content_type or '').split(';')[0].lower()
  if mime=='text/plain' or (file.filename or '').lower().endswith('.txt'):
   try:body=VoiceText(request_id=request_id,source=source,locale=locale,text=blob.decode('utf-8-sig').strip())
   except Exception:raise HTTPException(422,'TXT는 UTF-8, 최대 16,000자여야 합니다.')
   return await voice_text(body,_)
  if mime not in {'audio/wav','audio/x-wav','audio/wave','audio/mpeg','audio/mp4','audio/x-m4a','audio/webm','audio/ogg','audio/flac','audio/aac'}:raise HTTPException(415,'지원하지 않는 파일 형식입니다.')
  vid=uid();filename=vid+'.bin';path=data/'audio'/filename
  values={'id':vid,'request_id':validated.request_id,'source':validated.source,'kind':'audio','text':'','locale':validated.locale,'status':'awaiting_transcription','metadata':'{}','filename':filename,'media_type':mime,'digest':hashlib.sha256(blob+mime.encode()+locale.encode()).hexdigest(),'created_at':utcnow()}
  path.write_bytes(blob);path.chmod(0o600)
  try:result=insert_voice(values)
  except Exception:path.unlink(missing_ok=True);raise
  if result['duplicate']:path.unlink(missing_ok=True)
  else:await changed('voice.received','audio')
  return result
 @app.get('/api/voice/{vid}/audio')
 async def get_audio(vid:str,_=Depends(admin)):
  with store.connect() as db:row=db.execute('SELECT filename,media_type FROM voice WHERE id=?',(vid,)).fetchone()
  if not row or not row['filename']:raise HTTPException(404,'음성 파일이 없습니다.')
  return FileResponse(data/'audio'/row['filename'],media_type=row['media_type'],filename='voice-'+vid+'.bin')
 @app.patch('/api/voice/{vid}')
 async def review(vid:str,body:VoiceReview,_=Depends(admin)):
  speech.ensure_not_running(vid)
  with store.connect() as db:
   row=db.execute('SELECT * FROM voice WHERE id=?',(vid,)).fetchone()
   if not row:raise HTTPException(404,'입력 이벤트가 없습니다.')
   text=body.text if body.text is not None else row['text']
   if body.status=='pending_review' and not text.strip():raise HTTPException(422,'전사 텍스트가 필요합니다.')
   db.execute('UPDATE voice SET text=?,status=? WHERE id=?',(text,body.status,vid))
  await changed('voice.reviewed',vid);return {'ok':True}
 @app.delete('/api/voice/{vid}')
 async def delete_voice(vid:str,_=Depends(admin)):
  speech.ensure_not_running(vid)
  llm.ensure_voice_deletable(vid)
  with store.connect() as db:
   row=db.execute('SELECT filename FROM voice WHERE id=?',(vid,)).fetchone()
   if not row:raise HTTPException(404,'입력 이벤트가 없습니다.')
   db.execute('DELETE FROM voice WHERE id=?',(vid,))
  if row['filename']:(data/'audio'/row['filename']).unlink(missing_ok=True)
  await changed('voice.deleted',vid);return {'ok':True}
 @app.get('/api/llm/config')
 async def llm_config(_=Depends(admin)):return llm.status()
 @app.put('/api/llm/config')
 async def llm_config_set(body:LLMConfig,_=Depends(admin)):return await llm.configure(body)
 @app.post('/api/llm/probe')
 async def llm_probe(_=Depends(admin)):return await llm.probe()
 @app.get('/api/llm/requests')
 async def llm_list(limit:int=30,offset:int=0,status:str='',voice_id:str='',query:str='',_=Depends(admin)):
  if not 1<=limit<=100 or offset<0 or len(query)>240 or len(voice_id)>80:raise HTTPException(422,'조회 범위를 확인하세요.')
  return llm.listing(limit,offset,status,voice_id,query)
 @app.post('/api/llm/requests',status_code=202)
 async def llm_submit(body:LLMCreate,_=Depends(admin)):return await llm.submit(body)
 @app.get('/api/llm/requests/{rid}')
 async def llm_get(rid:str,_=Depends(admin)):return llm.get(rid)
 @app.get('/api/llm/requests/{rid}/export')
 async def llm_export(rid:str,_=Depends(admin)):
  row=llm.get(rid)
  return Response(json.dumps(row,ensure_ascii=False,indent=2),media_type='application/json',
   headers={'Content-Disposition':'attachment; filename="room-hub-llm-'+row['id']+'.json"'})
 @app.post('/api/llm/requests/{rid}/send')
 async def llm_send(rid:str,_=Depends(admin)):return await llm.run_prepared(rid)
 @app.post('/api/llm/requests/{rid}/retry',status_code=202)
 async def llm_retry(rid:str,body:LLMRetry,_=Depends(admin)):return await llm.retry(rid,body)
 @app.post('/api/assistant/{rid}/confirm')
 async def assistant_confirm(rid:str,body:Confirmation,_=Depends(admin)):return await llm.confirm_action(rid,body)
 @app.post('/api/llm/requests/{rid}/cancel')
 async def llm_cancel(rid:str,_=Depends(admin)):return await llm.cancel(rid)
 @app.delete('/api/llm/requests/{rid}')
 async def llm_delete(rid:str,_=Depends(admin)):return await llm.delete(rid)
 @app.get('/api/admin/integration')
 async def integration(_=Depends(admin)):return {'schema_version':'1','ingest_token':ingest_key,'max_audio_mb':10,'transcription_enabled':speech.status()['ready'],'auto_execute':False}
 @app.get('/api/speech/status')
 async def speech_status(_=Depends(viewer)):return speech.status()
 @app.get('/api/speech/jobs')
 async def speech_recent(session=Depends(viewer)):return {'jobs':speech.recent(session)}
 @app.post('/api/speech/jobs',status_code=202)
 async def speech_upload(file:UploadFile=File(...),request_id:str=Form(...),session=Depends(viewer)):
  blob=await file.read(10*1024*1024+1)
  if not blob or len(blob)>10*1024*1024:raise HTTPException(413,'파일 크기를 확인하세요. 최대 10MB입니다.')
  return await speech.submit(blob,file.content_type or '',request_id,session)
 @app.get('/api/speech/jobs/{jid}')
 async def speech_job(jid:str,session=Depends(viewer)):return speech.get(jid,session)
 @app.post('/api/speech/jobs/{jid}/cancel')
 async def speech_cancel(jid:str,session=Depends(viewer)):return await speech.cancel(jid,session)
 @app.post('/api/speech/jobs/{jid}/retry')
 async def speech_retry(jid:str,session=Depends(viewer)):return await speech.retry(jid,session)
 @app.post('/api/speech/enqueue/{vid}',status_code=202)
 async def speech_existing(vid:str,session=Depends(admin)):return await speech.enqueue_existing(vid,session)
 @app.get('/roomhub-ca.cer')
 async def local_ca():
  # Only a generated PUBLIC certificate is available; never mount data/ as static files.
  path=data/'https/public/roomhub-ca.cer'
  if not path.is_file():raise HTTPException(404,'HTTPS 인증서를 먼저 생성하세요.')
  return FileResponse(path,media_type='application/x-x509-ca-cert',filename='roomhub-ca.cer',headers={'Cache-Control':'no-store'})
 @app.websocket('/ws/{role}')
 async def websocket(ws:WebSocket,role:str):
  origin=ws.headers.get('origin')
  if role not in ['display','manager'] or (origin and urlparse(origin).netloc!=ws.headers.get('host')):await ws.close(code=1008);return
  a=read_session(ws.cookies.get('room_admin'));d=read_session(ws.cookies.get('room_display'));s=a if role=='manager' else d or a
  if not s or (role=='manager' and s['role']!='admin'):await ws.close(code=4001);return
  await ws.accept();m={'role':role,'device_id':s.get('device_id'),'view':'home','last_command':None,'expires_at':s['expires_at']};connections[ws]=m
  try:
   await ws.send_json({'type':'hello','server_time':utcnow(),'revision':store.get('revision')})
   while True:
    try:msg=await asyncio.wait_for(ws.receive_json(),65)
    except asyncio.TimeoutError:await ws.close(code=4000);break
    if time.time()>m['expires_at']:await ws.close(code=4001);break
    if not isinstance(msg,dict):continue
    if msg.get('type')=='ping':await ws.send_json({'type':'pong','server_time':utcnow(),'revision':store.get('revision')})
    elif msg.get('type')=='presence':
     m['view']=str(msg.get('view','home'))[:80]
     if m['device_id']:
      with store.connect() as db:db.execute('UPDATE devices SET last_seen=?,viewport=? WHERE id=?',(utcnow(),str(msg.get('viewport',''))[:40],m['device_id']))
    elif msg.get('type')=='ack':
     m['last_command']={'id':str(msg.get('id',''))[:80],'status':str(msg.get('status','rendered'))[:40],'at':utcnow()};await broadcast({'type':'device_ack','device_id':m['device_id'],**m['last_command']},role='manager')
  except (WebSocketDisconnect,RuntimeError,ValueError):pass
  finally:connections.pop(ws,None)
 app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static');app.mount('/widgets',StaticFiles(directory=widget_root,follow_symlink=False),name='widgets')
 return app

app=create_app()
