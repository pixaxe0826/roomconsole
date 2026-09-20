"""Speech API and worker safety. FakeRunner tests orchestration, NOT ASR accuracy.
Real FFmpeg tests plus a contract executable verify media and process plumbing.
"""
import asyncio
import contextlib
from dataclasses import replace, asdict
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import signal
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.speech import SpeechConfig, TranscriptionError, WhisperRunner, _run_process, inspect_wav, validate_audio


def wav_bytes(seconds=1, silent=False):
    out=io.BytesIO()
    with wave.open(out,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
        w.writeframes(b''.join(struct.pack('<h',0 if silent else int(4000*math.sin(i*.1))) for i in range(int(seconds*16000))))
    return out.getvalue()


class FakeRunner:
    def __init__(self):
        self.hold=threading.Event();self.calls=[];self.concurrent=0;self.peak=0;self.fail=False
        self.text='내일 택배 보내기 <script>not executable</script>'
    async def transcribe(self,source,cfg,cancel,temp_root):
        self.concurrent+=1;self.peak=max(self.peak,self.concurrent);self.calls.append(str(source))
        try:
            while self.hold.is_set():
                if cancel.is_set():raise asyncio.CancelledError
                await asyncio.sleep(.02)
            if self.fail:raise TranscriptionError('테스트 전사 실패')
            return self.text,1.0
        finally:self.concurrent-=1


@pytest.fixture
def enabled(tmp_path):
    model=tmp_path/'test-model.bin';model.write_bytes(b'x'*2048)
    cfg=SpeechConfig(enabled=True,binary=sys.executable,model=str(model),ffmpeg=sys.executable)
    runner=FakeRunner();app=create_app(tmp_path/'data',weather_enabled=False,speech_config=cfg,speech_runner=runner)
    with TestClient(app) as c:
        c.headers['X-Room-Request']='1'
        admin={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}
        p=c.post('/api/devices/pair',json={'name':'one'},headers=admin).json()
        assert c.post('/api/devices/claim',json={'code':p['path'].split('=')[1]}).status_code==200
        yield app,c,runner,admin,p


def post(c,request_id='audio-1',content=None,mime='audio/wav'):
    return c.post('/api/speech/jobs',data={'request_id':request_id},files={'file':('voice.wav',content if content is not None else wav_bytes(),mime)})


def wait(c,jid,statuses=('succeeded','failed','cancelled'),timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        r=c.get('/api/speech/jobs/'+jid);assert r.status_code==200,r.text
        row=r.json()
        if row['status'] in statuses:return row
        time.sleep(.025)
    raise AssertionError('job did not settle: '+str(row))


def wait_llm(app, voice_id, timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        with app.state.store.connect() as db:
            row=db.execute('SELECT id,status FROM llm_requests WHERE source_voice_id=? ORDER BY created_at DESC LIMIT 1',(voice_id,)).fetchone()
        if row and row['status'] not in {'queued','running'}:
            return app.state.llm.get(row['id'])
        time.sleep(.025)
    raise AssertionError('automatic assistant request did not settle')


def test_disabled_upload_no_file(tmp_path):
    a=create_app(tmp_path,weather_enabled=False)
    with TestClient(a) as c:
        c.headers.update({'Authorization':'Bearer '+a.state.admin_token,'X-Room-Request':'1'})
        assert not c.get('/api/speech/status').json()['ready']
        assert post(c).status_code==503
        assert not list((tmp_path/'audio').iterdir())


@pytest.mark.parametrize('route,method',[('/api/speech/status','GET'),('/api/speech/jobs','GET'),('/api/speech/jobs/unknown','GET'),('/api/speech/jobs/unknown/cancel','POST'),('/api/speech/jobs/unknown/retry','POST')])
def test_anonymous_denied(enabled,route,method):
    _,c,_,_,_=enabled;c.cookies.clear();assert c.request(method,route).status_code==401


def test_complete_idempotent_no_task_and_private(enabled):
    app,c,r,a,_=enabled
    before=c.get('/api/state').json()['tasks']
    first=post(c);assert first.status_code==202,first.text
    job=wait(c,first.json()['id']);assert job['text'].startswith('내일 택배 보내기')
    assert 'filename' not in job and 'digest' not in job and 'owner' not in job
    duplicate=post(c).json();assert duplicate['duplicate'] and duplicate['id']==job['id'] and len(r.calls)==1
    assert post(c,content=wav_bytes(.8)).status_code==409
    assert c.get('/api/state').json()['tasks']==before
    assert c.get('/api/voice/'+job['voice_id']+'/audio').status_code==401
    assert c.get('/api/voice/'+job['voice_id']+'/audio',headers=a).content==wav_bytes()
    voice=c.get('/api/admin/overview',headers=a).json()['voice'][0]
    assert voice['job_status']=='succeeded' and voice['status']=='pending_review'



def test_success_auto_submits_exactly_once_and_fast_read_skips_model(enabled):
    app,c,r,a,_=enabled
    r.text='오늘 할 일 확인해줘'
    assert c.get('/api/speech/status').json()['auto_submit_llm'] is True
    first=post(c,'auto-fast');assert first.status_code==202,first.text
    job=wait(c,first.json()['id'])
    req=wait_llm(app,job['voice_id'])
    assert req['status']=='succeeded'
    assert req['source_text']==r.text and req['assistant']['mode']=='auto'
    assert req['assistant']['routing']['route']=='FAST_PATH'
    assert req['assistant']['routing']['llm_called'] is False
    assert not req['dispatch_attempted']
    duplicate=post(c,'auto-fast').json()
    assert duplicate['duplicate'] and duplicate['id']==job['id']
    with app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM llm_requests WHERE source_voice_id=?',(job['voice_id'],)).fetchone()[0]==1


def test_auto_submit_write_still_requires_confirmation(enabled):
    app,c,r,a,_=enabled
    r.text='오늘 할 일에 우유 사기 추가해'
    job=wait(c,post(c,'auto-write').json()['id'])
    req=wait_llm(app,job['voice_id'])
    assert req['status']=='awaiting_confirmation'
    assert req['assistant']['proposal']['intent']=='todo.create'
    assert not app.state.store.tasks()


def test_auto_submit_failure_does_not_rewrite_successful_stt(enabled):
    app,c,r,a,_=enabled
    async def broken(*_):
        raise RuntimeError('test-only sink failure')
    app.state.speech.set_transcript_sink(broken)
    job=wait(c,post(c,'auto-sink-fail').json()['id'])
    assert job['status']=='succeeded' and job['text']
    assert 'Assistant 자동 전달에 실패' in app.state.speech.last_error
    with app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM llm_requests').fetchone()[0]==0


def test_device_ownership_and_revoke(enabled):
    app,c,r,a,p=enabled;job=post(c).json();wait(c,job['id'])
    p2=c.post('/api/devices/pair',headers=a,json={'name':'two'}).json()
    c.cookies.clear();c.post('/api/devices/claim',json={'code':p2['path'].split('=')[1]})
    assert c.get('/api/speech/jobs').json()['jobs']==[]
    for suffix,method in [('', 'GET'),('/cancel','POST'),('/retry','POST')]:
        assert c.request(method,'/api/speech/jobs/'+job['id']+suffix).status_code==404
    assert c.post('/api/speech/enqueue/'+job['voice_id']).status_code==401
    assert c.get('/api/speech/jobs/'+job['id'],headers=a).status_code==200
    c.delete('/api/devices/'+p2['device_id'],headers=a)
    assert c.get('/api/speech/status').status_code==401


def test_queue_sequential_cancel_and_limits(enabled):
    app,c,r,a,_=enabled;r.hold.set()
    jobs=[post(c,'req-'+str(i)).json() for i in range(4)]
    wait(c,jobs[0]['id'],('running',))
    assert post(c,'overflow').status_code==429
    assert post(c,'req-0').json()['duplicate']
    assert c.get('/healthz').status_code==200
    assert c.post('/api/speech/jobs/'+jobs[-1]['id']+'/cancel').json()['status']=='cancelled'
    assert c.post('/api/speech/jobs/'+jobs[-1]['id']+'/cancel').json()['status']=='cancelled'
    r.hold.clear()
    for job in jobs[:3]:assert wait(c,job['id'])['status']=='succeeded'
    assert len(r.calls)==3 and r.peak==1


def test_cancel_running_and_retry(enabled):
    app,c,r,a,_=enabled;r.hold.set();j=post(c).json();wait(c,j['id'],('running',))
    assert c.patch('/api/voice/'+j['voice_id'],headers=a,json={'status':'reviewed'}).status_code==409
    assert c.delete('/api/voice/'+j['voice_id'],headers=a).status_code==409
    assert c.post('/api/speech/jobs/'+j['id']+'/cancel').json()['status']=='cancelled'
    time.sleep(.12);r.hold.clear()
    res=c.post('/api/speech/jobs/'+j['id']+'/retry');assert res.status_code==200,res.text
    assert wait(c,j['id'])['attempts']==2
    assert c.post('/api/speech/jobs/'+j['id']+'/retry').status_code==409
    assert c.delete('/api/voice/'+j['voice_id'],headers=a).status_code==200
    assert c.get('/api/speech/jobs/'+j['id']).status_code==404
    assert not list((app.state.store.path.parent/'audio').iterdir())


def test_failed_no_automatic_retry(enabled):
    app,c,r,a,_=enabled;r.fail=True;j=post(c).json();assert wait(c,j['id'])['status']=='failed'
    time.sleep(.1);assert len(r.calls)==1
    with app.state.store.connect() as db:assert db.execute('SELECT count(*) FROM llm_requests').fetchone()[0]==0
    for n in [2,3]:
        assert c.post('/api/speech/jobs/'+j['id']+'/retry').status_code==200
        assert wait(c,j['id'])['attempts']==n
    assert c.post('/api/speech/jobs/'+j['id']+'/retry').status_code==409
    r.fail=False
    assert c.patch('/api/voice/'+j['voice_id'],headers=a,json={'status':'pending_review','text':'관리자 수정'}).status_code==200
    assert c.get('/api/speech/jobs/'+j['id']).json()['text']=='관리자 수정'


@pytest.mark.parametrize('request_id',['../escape','a'*129,'with space','<bad>'])
def test_bad_request_id(enabled,request_id):assert post(enabled[1],request_id).status_code==422


@pytest.mark.parametrize('mime,blob',[('text/html',b'<script>test</script>'),('audio/wav',b'not an audio file'),('audio/webm',b'<script>alert(1)</script>')])
def test_reject_non_audio(enabled,mime,blob):assert post(enabled[1],content=blob,mime=mime).status_code==415


def test_csrf_and_oversize(enabled):
    app,c,r,a,_=enabled
    assert c.post('/api/speech/jobs',headers={'Origin':'https://evil.example'},data={'request_id':'x'},files={'file':('x.wav',wav_bytes(),'audio/wav')}).status_code==403
    assert post(c,content=b'RIFF'+b'0'*(9*1024*1024)).status_code==413
    app.state.speech.override=replace(app.state.speech.override,max_stored_bytes=1024)
    assert post(c).status_code==507
    assert not r.calls


def test_admin_enqueue_old_upload(enabled):
    app,c,r,a,_=enabled
    res=c.post('/api/voice/upload',headers=a,data={'request_id':'legacy'},files={'file':('x.wav',wav_bytes(),'audio/wav')})
    assert res.status_code==202 and res.json()['status']=='awaiting_transcription'
    vid=res.json()['id'];j=c.post('/api/speech/enqueue/'+vid,headers=a).json()
    while True:
        row=c.get('/api/speech/jobs/'+j['id'],headers=a).json()
        if row['status']=='succeeded':break
        time.sleep(.02)
    assert row['voice_id']==vid and len(r.calls)==1


def test_interrupted_worker_preserves_data(tmp_path):
    model=tmp_path/'model';model.write_bytes(b'0'*2048);cfg=SpeechConfig(enabled=True,binary=sys.executable,model=str(model),ffmpeg=sys.executable)
    runner=FakeRunner();runner.hold.set()
    app=create_app(tmp_path/'data',weather_enabled=False,speech_config=cfg,speech_runner=runner)
    with TestClient(app) as c:
        c.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
        j=post(c).json();wait(c,j['id'],('running',))
    again=create_app(tmp_path/'data',weather_enabled=False,speech_config=cfg,speech_runner=FakeRunner())
    with TestClient(again) as c:
        c.headers['Authorization']='Bearer '+again.state.admin_token
        row=c.get('/api/speech/jobs/'+j['id']).json()
        assert row['status']=='failed' and not row['text'] and row['attempts']==1
        assert again.state.admin_token==app.state.admin_token


def test_security_headers_and_ca(enabled):
    app,c,r,a,_=enabled
    resp=c.get('/client');assert 'microphone=(self)' in resp.headers['permissions-policy']
    assert "media-src 'self' blob:" in resp.headers['content-security-policy']
    assert 'speech.js?v=0.1.3' in resp.text
    assert c.get('/roomhub-ca.cer').status_code==404
    pub=app.state.store.path.parent/'https/public';pub.mkdir(parents=True);(pub/'roomhub-ca.cer').write_bytes(b'PUBLIC')
    assert c.get('/roomhub-ca.cer').content==b'PUBLIC'
    assert c.get('/data/https/private/ca.key').status_code==404
    assert c.get('/static/../data/https/private/ca.key').status_code==404


@pytest.mark.parametrize('change',[{'threads':9},{'threads':True},{'max_seconds':600},{'enabled':'yes'},{'binary':'relative'},{'language':'$(id)'},{'unknown':'x'},{'max_pending':0}])
def test_bad_config(tmp_path,change):
    p=tmp_path/'speech-config.json';p.write_text(json.dumps(change))
    with pytest.raises((ValueError,TypeError)):SpeechConfig.read(p)


def test_missing_bad_config_readiness(tmp_path):
    app=create_app(tmp_path,weather_enabled=False);(tmp_path/'speech-config.json').write_text('invalid')
    with TestClient(app) as c:
        c.headers['Authorization']='Bearer '+app.state.admin_token
        assert not c.get('/api/speech/status').json()['ready']


def contract_cli(tmp_path):
    p=tmp_path/'contract-cli'
    p.write_text('#!'+sys.executable+'\nimport sys,pathlib\na=sys.argv\nassert a[a.index("-l")+1]=="ko"\nassert "-ng" in a and a[a.index("-t")+1]=="2"\npathlib.Path(a[a.index("-of")+1]+".txt").write_text("전사 계약 테스트",encoding="utf-8")\n')
    p.chmod(0o700);model=tmp_path/'model';model.write_bytes(b'0'*2048)
    return SpeechConfig(enabled=True,binary=str(p),model=str(model))


@pytest.mark.parametrize('extension,codec,mime',[('wav','pcm_s16le','audio/wav'),('m4a','aac','audio/mp4'),('webm','libopus','audio/webm')])
@pytest.mark.skipif(os.name!='posix' or not shutil.which('ffmpeg'),reason='Requires Linux/POSIX and real FFmpeg')
def test_real_ffmpeg_and_cli_contract(tmp_path,extension,codec,mime):
    source=tmp_path/'source.wav';source.write_bytes(wav_bytes())
    audio=tmp_path/('recording.'+extension)
    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-i',str(source),'-c:a',codec,'-y',str(audio)],check=True)
    validate_audio(audio.read_bytes(),mime)
    text,seconds=asyncio.run(WhisperRunner().transcribe(audio,contract_cli(tmp_path),asyncio.Event(),tmp_path/'work'))
    assert text=='전사 계약 테스트' and .9<seconds<1.2
    assert not list((tmp_path/'work').iterdir())


@pytest.mark.parametrize('seconds,silent,reason',[(.1,False,'짧'),(4,False,'최대'),(1,True,'소리')])
@pytest.mark.skipif(os.name!='posix' or not shutil.which('ffmpeg'),reason='Requires Linux/POSIX and real FFmpeg')
def test_real_ffmpeg_limits(tmp_path,seconds,silent,reason):
    source=tmp_path/'source.wav';source.write_bytes(wav_bytes(seconds,silent))
    cfg=replace(contract_cli(tmp_path),max_seconds=3)
    with pytest.raises(TranscriptionError,match=reason):
        asyncio.run(WhisperRunner().transcribe(source,cfg,asyncio.Event(),tmp_path/'tmp'))
    assert not list((tmp_path/'tmp').iterdir())


@pytest.mark.skipif(os.name!='posix' or not shutil.which('ffmpeg'),reason='Requires Linux/POSIX and real FFmpeg')
def test_process_timeout_and_cancel(tmp_path):
    script=tmp_path/'sleep.py';pid=tmp_path/'pid';script.write_text('import os,time,pathlib,sys\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(30)\n')
    async def timed():
        with pytest.raises(TranscriptionError):await _run_process([sys.executable,str(script),str(pid)],asyncio.Event(),2)
    asyncio.run(timed())
    with pytest.raises(ProcessLookupError):os.kill(int(pid.read_text()),0)
    async def cancelled():
        cancel=asyncio.Event();task=asyncio.create_task(_run_process([sys.executable,str(script),str(pid)],cancel,20));await asyncio.sleep(.8);cancel.set()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(cancelled())
    with pytest.raises(ProcessLookupError):os.kill(int(pid.read_text()),0)


def test_stop_target_selection():
    p=Path(__file__).resolve().parents[1]/'deploy/termux/stop-room-hub.py'
    spec=importlib.util.spec_from_file_location('stop_test',p);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    root=Path('/data/data/com.termux/files/home/room-hub')
    assert mod.matching(['/opt/room-hub/.venv-v35/bin/python','/opt/room-hub/deploy/termux/server.py'],root)
    for args in [['sshd'],['nginx'],['cloudflared','tunnel','run'],['python','other.py'],['proot','--bind','/opt/room-hub/deploy/termux/server.py']]:
        assert not mod.matching(args,root)
