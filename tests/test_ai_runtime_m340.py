"""Isolated HTTP lifecycle tests with simulated remote availability only."""
import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.llm import ChatBackend, LLMConfig
from app.main import create_app
from app.speech import SpeechConfig
from scripts.configure_remote_speech import configure
from test_speech import wav_bytes, post, wait


@pytest.fixture
def environment(tmp_path, monkeypatch):
    calls=[]; online={'speech': True, 'llm': True}
    model='synthetic-llm-model'
    async def handler(req):
        calls.append((req.url.port,req.method,req.url.path))
        kind='speech' if req.url.port==8178 else 'llm'
        if not online[kind]:
            raise httpx.ConnectError('private test endpoint detail',request=req)
        if req.url.path=='/health':
            return httpx.Response(200,json={'status':'ok'})
        if req.url.path=='/v1/models':
            return httpx.Response(200,json={'data':[{'id':model}]})
        if req.url.path=='/inference':
            return httpx.Response(200,json={'text':'합성 음성 결과.', 'segments':[]})
        return httpx.Response(500,json={'error':'unexpected endpoint'})
    async def prepare(source,cfg,cancel,dest):
        dest.write_bytes(b'RIFF'+b'\x01'*40)
        return 1.0
    monkeypatch.setattr('app.remote_speech.prepare_wav',prepare)
    cfg=SpeechConfig(enabled=True,backend='remote_http',remote_url='http://127.0.0.1:8178/inference',
                     remote_model='synthetic-stt-model',remote_device='cuda',ffmpeg=sys.executable)
    backend=ChatBackend(httpx.MockTransport(handler))
    app=create_app(tmp_path,weather_enabled=False,speech_config=cfg,llm_backend=backend)
    app.state.speech.remote_runner.transport=httpx.MockTransport(handler)
    # This synthetic backend will not answer any actual LLM generation request.
    app.state.store.set('llm_config',LLMConfig(enabled=False,model=model).model_dump())
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+app.state.admin_token, 'X-Room-Request':'1'}
        client.headers.update(headers)
        yield app,client,calls,online,tmp_path


def test_status_read_does_not_probe_and_page_does_not_expose_keys(environment):
    app,c,calls,online,tmp=environment
    status=c.get('/api/ai/status').json()
    assert status['speech']['state']=='unknown' and status['llm']['state']=='unknown'
    assert calls==[]
    response=c.get('/manager/runtime')
    assert response.status_code==200 and '/static/runtime.js' in response.text
    assert app.state.admin_token not in response.text
    assert c.get('/api/speech/status').json()['model']=='synthetic-stt-model'
    assert calls==[]


def test_probe_only_checks_health_and_model_list_and_invalidates_cache(environment):
    app,c,calls,online,tmp=environment
    report=c.post('/api/ai/probe').json()
    assert report['speech']['state']=='online'
    assert report['speech']['reported_model'] is None and report['speech']['reported_device'] is None
    assert report['speech']['model_verified'] is False
    assert report['llm']['model_list_match'] is True
    assert sorted(calls)==[(8090,'GET','/v1/models'),(8178,'GET','/health')]
    c.get('/api/ai/status'); assert len(calls)==2
    app.state.speech._probe_time-=31;app.state.ai_runtime.llm_probe_time-=31
    stale=c.get('/api/ai/status').json()
    assert stale['speech']['state']=='unknown' and stale['llm']['stale']
    c.post('/api/ai/probe')
    app.state.speech.override=replace(app.state.speech.override,remote_model='different-label')
    assert c.get('/api/ai/status').json()['speech']['last_probe'] is None


@pytest.mark.parametrize('path,method', [('/api/ai/status','GET'),('/api/ai/probe','POST'),('/manager/runtime','GET')])
def test_runtime_diagnostics_manager_only(environment,path,method):
    app,c,calls,online,tmp=environment
    c.headers.pop('Authorization');c.cookies.clear()
    assert c.request(method,path).status_code==401
    c.headers['Authorization']='Bearer '+app.state.ingest_token
    assert c.request(method,path).status_code==401
    c.headers['Authorization']='Bearer '+app.state.admin_token
    pair=c.post('/api/devices/pair',json={'name':'synthetic-panel'}).json()
    c.headers.pop('Authorization')
    c.post('/api/devices/claim',json={'code':pair['path'].split('=')[1]})
    assert c.request(method,path).status_code==401
    assert calls==[]


def test_offline_core_works_failed_speech_not_replayed_recovery_new_request(environment):
    app,c,calls,online,tmp=environment
    online.update(speech=False,llm=False)
    status=c.post('/api/ai/probe').json()
    assert status['speech']['state']=='offline' and status['llm']['state']=='offline'
    assert c.get('/healthz').json()['status']=='ok'
    assert c.get('/api/state').status_code==200
    task=c.post('/api/tasks',json={'title':'합성 보관함 정리','date':'2026-11-02'});assert task.status_code==201,task.text
    note=c.post('/api/life/notes',json={'title':'합성 기록','body':'시험용 내용','request_id':'synthetic-note-offline'});assert note.status_code==201,note.text
    pair=c.post('/api/devices/pair',json={'name':'offline-screen'}).json()
    assert c.delete('/api/devices/'+pair['device_id']).status_code==200
    app.state.life.tick(); app.state.timers.tick()
    first=post(c,request_id='synthetic-remote-fail');assert first.status_code==202,first.text
    job=wait(c,first.json()['id']);assert job['status']=='failed'
    assert 'private' not in job['error']
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM llm_requests').fetchone()[0]==0
    sent=sum(path=='/inference' for _,_,path in calls)
    online.update(speech=True,llm=True)
    c.post('/api/ai/probe')
    assert c.get('/api/speech/jobs/'+job['id']).json()['status']=='failed'
    assert sum(path=='/inference' for _,_,path in calls)==sent
    second=post(c,request_id='synthetic-remote-new');assert second.status_code==202
    new=wait(c,second.json()['id']);assert new['status']=='succeeded'
    with app.state.store.connect() as db:
        reports=[json.loads(row[0]) for row in db.execute('SELECT report FROM speech_diagnostics')]
        assert len(reports)==1 and reports[0]['backend']=='remote_http'
    assert c.get('/healthz').status_code==200


def test_llm_model_mismatch_is_not_connection_success(environment):
    app,c,calls,online,tmp=environment
    cfg=app.state.llm.config().model_dump();cfg['model']='unlisted-model'
    app.state.store.set('llm_config',cfg)
    report=c.post('/api/ai/probe').json()
    assert report['llm']['state']=='model_mismatch'
    assert report['llm']['model_list_match'] is False


def test_config_migration_dry_run_backup_preserves_policy(tmp_path):
    config=SpeechConfig(enabled=True,binary='/opt/synthetic-wrapper',model='/opt/unused-model.bin',ffmpeg=sys.executable,threads=6)
    path=tmp_path/'speech-config.json';path.write_text(json.dumps(asdict(config)),encoding='utf-8');before=path.read_bytes()
    policy=tmp_path/'stt-accuracy.json';policy.write_text('{"profile":"careful"}');before_policy=policy.read_bytes()
    kwargs=dict(endpoint='http://127.0.0.1:8178/inference',model='medium',device='cuda')
    dry=configure(tmp_path,**kwargs)
    assert not dry['apply'] and path.read_bytes()==before
    result=configure(tmp_path,**kwargs,apply=True)
    assert Path(result['backup']).read_bytes()==before
    assert policy.read_bytes()==before_policy
    after=SpeechConfig.read(path)
    assert after.backend=='remote_http' and after.enabled and after.threads==6
    assert after.binary==config.binary and after.model==config.model
    assert after.readiness()[0]
    assert result['service_restart']=='not_run'


def test_bad_migration_no_overwrite(tmp_path):
    path=tmp_path/'speech-config.json';path.write_text(json.dumps(asdict(SpeechConfig())))
    before=path.read_bytes()
    with pytest.raises(ValueError):
        configure(tmp_path,endpoint='http://192.0.2.2:8178/inference',model='medium',device='cuda',apply=True)
    assert path.read_bytes()==before and not list(tmp_path.glob('*.before-*'))


def test_llm_offline_attempt_not_replayed_and_core_stays_online(environment):
    from test_llm import enable, voice, submit, wait as wait_for_llm
    app,c,calls,online,tmp=environment
    online['llm']=False
    enable(c)
    text='합성 대화 연결 확인입니다.'
    vid=voice(c,text=text,key='synthetic-llm-offline')
    response=submit(c,vid,text=text,key='synthetic-llm-request')
    assert response.status_code==202,response.text
    result=wait_for_llm(c,response.json()['id'])
    assert result['status']=='failed' and result['error_code']=='connection_failed'
    assert result['dispatch_attempted'] is True
    assert c.get('/api/state').status_code==200
    attempts=sum(path=='/v1/chat/completions' for _,_,path in calls)
    online['llm']=True
    c.post('/api/ai/probe')
    assert c.get('/api/llm/requests/'+result['id']).json()['status']=='failed'
    assert sum(path=='/v1/chat/completions' for _,_,path in calls)==attempts==1


def test_probe_cannot_verify_model_changed_during_request(environment, monkeypatch):
    app,c,calls,online,tmp=environment
    original=app.state.llm.backend.probe
    async def changing(cfg):
        result=await original(cfg)
        updated=cfg.model_dump();updated['model']='different-during-probe'
        app.state.store.set('llm_config',updated)
        return result
    monkeypatch.setattr(app.state.llm.backend,'probe',changing)
    report=c.post('/api/ai/probe').json()
    assert report['llm']['state']=='probe_error'
    cached=c.get('/api/ai/status').json()['llm']
    assert cached['state']=='unknown' and not cached['model_list_match']
    assert cached['configured_model']=='different-during-probe'


def test_invalid_stt_config_does_not_break_core(environment):
    app,c,calls,online,tmp=environment
    app.state.speech.override=None
    (tmp/'speech-config.json').write_text('{"backend":"remote_http","remote_url":"http://192.0.2.1:8178/inference"}')
    assert c.get('/api/ai/status').json()['speech']['state']=='config_error'
    assert c.get('/healthz').json()['status']=='ok'
    assert c.get('/api/state').status_code==200
    assert c.get('/api/speech/status').json()['ready'] is False
    assert calls==[]


def test_windows_diagnostic_script_parses_without_execution():
    import os
    import shutil
    import subprocess
    powershell=shutil.which('pwsh') or shutil.which('powershell')
    if not powershell:
        pytest.skip('PowerShell parser is checked in Windows CI')
    path=Path(__file__).resolve().parents[1]/'deploy/windows/RoomHub_AI_Diagnostics.ps1'
    env=dict(os.environ, ROOM_HUB_TEST_PS1=str(path))
    command="$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile($env:ROOM_HUB_TEST_PS1,[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count){$errors | Out-String | Write-Error; exit 1}"
    subprocess.run([powershell,'-NoProfile','-NonInteractive','-Command',command],env=env,
                   check=True,capture_output=True,text=True,timeout=30)
