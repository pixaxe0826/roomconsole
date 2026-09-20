"""Full-list release: registry, existing data, completion permissions and no truncation."""
from datetime import date,timedelta
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.store import DEFAULT_LAYOUT
import pytest

@pytest.fixture
def hub(tmp_path):
 app=create_app(tmp_path,weather_enabled=False)
 with TestClient(app) as admin,TestClient(app) as display:
  admin.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
  display.headers['X-Room-Request']='1'
  pair=admin.post('/api/devices/pair',json={'name':'Test all tasks display'}).json()
  assert display.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]}).status_code==200
  yield app,admin,display

def test_release_health_and_cache_keys(hub):
 _,a,d=hub
 assert a.get('/healthz').json()['version']=='0.1.7'
 assert 'shared.js?v=0.1.3' in d.get('/client').text
 assert 'manager.js?v=0.1.7' in a.get('/manager').text

def test_new_folder_discovered_without_altering_layout(hub):
 _,a,d=hub;s=d.get('/api/state').json()
 assert s['layout']==DEFAULT_LAYOUT
 assert {'todos','all-todos'}<={m['id'] for m in s['widgets']}
 assert len(s['widgets'])==8 and not s['widget_errors']
 for path in ['manifest.json','widget.js','style.css']:
  assert a.get('/widgets/all-todos/'+path).status_code==200

def test_select_type_preserves_position_and_task_data(hub):
 _,a,d=hub
 a.post('/api/tasks',json={'title':'keep me','date':'2026-09-01'})
 before=d.get('/api/state').json();l=before['layout'];l['widgets'][2]['type']='all-todos'
 assert a.put('/api/layout',json=l).status_code==200
 after=d.get('/api/state').json()
 assert after['tasks']==before['tasks'] and after['layout']['widgets'][2]['x']==0
 assert d.put('/api/layout',json=l).status_code==401

@pytest.mark.parametrize('count',[301,640,10000])
def test_state_contains_every_registered_task_and_completion_is_one_occurrence(hub,count):
 app,a,d=hub;now='2026-09-19T00:00:00Z'
 # Synthetic data only. Cover server maximum without repeated fixture HTTP overhead.
 with app.state.store.connect() as db:
  db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',[(f'bulk-{i:05d}',f'Test {i}',(date(2025,1,1)+timedelta(days=i//10)).isoformat(),'10:00','personal','normal','',i%2,None,1,now,now) for i in range(count)])
 s=d.get('/api/state').json();assert len(s['tasks'])==count
 assert len({t['id'] for t in s['tasks']})==count
 last=s['tasks'][-1];new=not last['completed']
 res=d.patch(f"/api/tasks/{last['id']}/completion",json={'completed':new,'version':1})
 assert res.status_code==200 and res.json()['completed']==new
 rows=a.get('/api/state').json()['tasks'];assert len(rows)==count
 assert next(t for t in rows if t['id']==last['id'])['version']==2
 assert d.patch('/api/tasks/'+last['id'],json={'title':'forbidden','version':2}).status_code==401
