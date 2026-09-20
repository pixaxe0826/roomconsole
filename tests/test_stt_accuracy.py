"""Synthetic audio + fake recognizer contract tests. NOT ASR quality benchmarks."""
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import wave
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.speech import SpeechConfig, WhisperRunner, TranscriptionError
from app.store import Store, utcnow
from app.stt_accuracy import AccuracyConfig, make_hint, task_hint, score_summary, diagnostic_segments, serialize_report, MAX_HINT_BYTES


def wav_blob():
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'))
        w.writeframes(struct.pack('<'+'h'*16000,*[900 if i%30<15 else -900 for i in range(16000)]))
    return b.getvalue()


def cli_fixture(root,mode='ok'):
    root.mkdir(parents=True,exist_ok=True)
    cli=root/'whisper-cli';model=root/'ggml-base.bin';model.write_bytes(b'MOCK GGML NOT A MODEL'+b'x'*4096)
    cli.write_text(f'''#!{sys.executable}
import sys,json,pathlib,time
args=sys.argv[1:]
if '--help' in args:
 print('--beam-size --prompt --output-json --log-score');sys.exit(0)
root=pathlib.Path(__file__).parent
(root/'args.json').write_text(json.dumps(args))
out=pathlib.Path(args[args.index('-of')+1])
if {mode!r}=='sleep': time.sleep(8)
beam=args[args.index('-bs')+1]
text='오늘 라면 달릴 확인해줘' if beam=='1' and '--prompt' not in args else '오늘 남은 할 일 확인해줘'
if '-l' in args and args[args.index('-l')+1]=='en': text='ask what your country can do for you'
if {mode!r}=='empty': text=''
out.with_suffix('.txt').write_text(text,encoding='utf-8')
if {mode!r}!='no_diag':
 if '-oj' in args:out.with_suffix('.json').write_text(json.dumps({{'params':{{'model':'PRIVATE/PATH'}},'result':{{'language':'ko'}},'transcription':[{{'text':text,'offsets':{{'from':0,'to':1000}}}}]}}),encoding='utf-8')
 if '-ls' in args:out.with_suffix('.score.txt').write_bytes(b'[_BEG_]\\t0.99\\n'+bytes([0xe3,0x85])+b'\\t0.8\\nword\\t0.4\\n[_EOT_]\\t0.99\\n')
''')
    cli.chmod(0o700)
    return SpeechConfig(enabled=True,binary=str(cli),model=str(model),model_name='base · multilingual',ffmpeg=shutil.which('ffmpeg') or '',threads=6)


@pytest.mark.parametrize('profile,beam',[('legacy',1),('hint',1),('balanced',3),('careful',5)])
def test_profiles(profile,beam):
    c=AccuracyConfig.from_dict({'profile':profile});assert c.beam==beam
    assert c.public()['automatic_second_pass'] is False


@pytest.mark.parametrize('bad',[{'profile':'invalid'},{'profile':[]},{'schema':True},{'schema':2},{'include_task_titles':'yes'},{'max_task_titles':True},{'max_task_titles':5},{'max_task_titles':-1},{'temperature':1},[],None])
def test_bad_policy(bad):
    with pytest.raises((ValueError,TypeError)):AccuracyConfig.from_dict(bad)


def test_missing_legacy_and_bounded_prompt(tmp_path):
    assert AccuracyConfig.read(tmp_path/'none').profile=='legacy'
    assert make_hint(AccuracyConfig(),['secret'])==('',[])
    text,titles=make_hint(AccuracyConfig(profile='balanced'), ['AI 라벨링','Apple Vision 정리','택배 보내기','과제 제출']*10)
    assert len(text.encode())<=MAX_HINT_BYTES and len(titles)<=4 and 'AI 라벨링' in text
    assert text.count('AI 라벨링')==1
    assert 'secret' not in make_hint(AccuracyConfig(profile='balanced',include_task_titles=False),['secret'])[0]
    assert not make_hint(AccuracyConfig(profile='hint',max_task_titles=0),['secret'])[1]


@pytest.mark.parametrize('bad',['<|startoftranscript|>','a\nb','a\x00b','a\u202eb','x'*41,'$(touch /tmp/x)','<script>alert(1)</script>',' '])
def test_title_rejected(bad):
    _,titles=make_hint(AccuracyConfig(profile='balanced'),[bad]);assert not titles


def insert_task(store,title,date,complete=0):
    import uuid
    now=utcnow()
    with store.connect() as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,title,date,None,'personal','normal','',complete,None,1,now,now))


def test_dynamic_today_tomorrow_privacy(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    insert_task(store,'오늘 할일','2026-09-20')
    insert_task(store,'내일 할일','2026-09-21')
    insert_task(store,'먼 미래 비공개','2026-10-03')
    insert_task(store,'완료된 비공개','2026-09-20',1)
    h=task_hint(store,AccuracyConfig(profile='balanced'),datetime(2026,9,19,19,30,tzinfo=timezone.utc))
    assert h['titles']==['오늘 할일','내일 할일']
    assert '먼 미래' not in h['text'] and '완료된' not in h['text']


def test_invalid_timezone_falls_back_to_fixed_hint(tmp_path):
    store=Store(tmp_path/'db');store.set('settings',{'timezone':'invalid/zone'})
    assert task_hint(store,AccuracyConfig(profile='balanced'))['warning']=='task_hint_unavailable'


def test_scores_are_not_accuracy(tmp_path):
    p=tmp_path/'score'
    assert not score_summary(p)['available']
    p.write_bytes(b'[_EOT_]\t.99\nfoo\t.8\n'+bytes([0xe3])+b'\t0.2\nbad\tNaN\nbad\t1.5\n')
    s=score_summary(p);assert s['available'] and s['token_count']==2
    assert s['mean_token_p']==pytest.approx(.5) and s['low_p_fraction']==.5
    assert 'NOT' in s['meaning']


def test_diagnostics_json_is_optional_private_and_bounded(tmp_path):
    p=tmp_path/'out.json';p.write_text('{broken')
    assert not diagnostic_segments(p)['available']
    p.write_text(json.dumps({'systeminfo':'secret','params':{'model':'secret'},'result':{'language':'ko'},'transcription':[{'text':'hello','offsets':{'from':0,'to':100}}]}))
    data=diagnostic_segments(p);assert data['available'] and 'secret' not in json.dumps(data)
    out=serialize_report({'raw_transcript':'가'*16000,'selected_transcript':'가'*16000,'segments':'가'*500000})
    assert len(out.encode())<256*1024 and 'report_size_limit' in out


@pytest.mark.parametrize('profile,beam,prompt',[('legacy','1',False),('hint','1',True),('balanced','3',True),('careful','5',True)])
def test_actual_subprocess_ffmpeg_and_cli_contract(tmp_path,profile,beam,prompt):
    cfg=cli_fixture(tmp_path/'bin');src=tmp_path/'source.wav';src.write_bytes(wav_blob())
    policy=AccuracyConfig(profile=profile);hint=dict(zip(['text','titles'],make_hint(policy,['테스트 할 일'])))
    text,duration,report=asyncio.run(WhisperRunner().transcribe_detailed(src,cfg,asyncio.Event(),tmp_path/'tmp',policy,hint))
    args=json.loads((Path(cfg.binary).parent/'args.json').read_text())
    assert args[args.index('-t')+1]=='6' and args[args.index('-p')+1]=='1' and '-ng' in args
    assert args[args.index('-bs')+1]==beam and args[args.index('-bo')+1]=='1'
    assert ('--prompt' in args)==prompt and ('-ls' in args)==prompt and '-ojf' not in args
    assert '-nf' not in args and '-tp' not in args
    assert not list((tmp_path/'tmp').iterdir())
    assert duration==1 and report['raw_transcript']==text==report['selected_transcript']
    assert report['threads']==6 and report['passes']==1 and not report['automatic_second_pass']
    assert report['scores']['available']==prompt


def test_legacy_interface_unchanged(tmp_path):
    cfg=cli_fixture(tmp_path/'bin');src=tmp_path/'audio.wav';src.write_bytes(wav_blob())
    text,duration=asyncio.run(WhisperRunner().transcribe(src,cfg,asyncio.Event(),tmp_path/'tmp'))
    args=json.loads((Path(cfg.binary).parent/'args.json').read_text())
    assert text=='오늘 라면 달릴 확인해줘' and '--prompt' not in args and '-ls' not in args


def test_diagnostic_missing_keeps_text(tmp_path):
    cfg=cli_fixture(tmp_path/'bin','no_diag');src=tmp_path/'audio.wav';src.write_bytes(wav_blob())
    text,_,rep=asyncio.run(WhisperRunner().transcribe_detailed(src,cfg,asyncio.Event(),tmp_path/'tmp',AccuracyConfig(profile='balanced'),{'text':'할 일'}))
    assert text and not rep['scores']['available'] and not rep['segments']['available']


def test_empty_result_no_fabrication(tmp_path):
    cfg=cli_fixture(tmp_path/'bin','empty');src=tmp_path/'audio.wav';src.write_bytes(wav_blob())
    with pytest.raises(TranscriptionError):
        asyncio.run(WhisperRunner().transcribe_detailed(src,cfg,asyncio.Event(),tmp_path/'tmp',AccuracyConfig(profile='balanced'),{'text':'할 일'}))


def test_cancel_preserved(tmp_path):
    cfg=cli_fixture(tmp_path/'bin','sleep');src=tmp_path/'audio.wav';src.write_bytes(wav_blob())
    async def run():
        cancel=asyncio.Event();task=asyncio.create_task(WhisperRunner().transcribe_detailed(src,cfg,cancel,tmp_path/'tmp',AccuracyConfig(profile='balanced'),{'text':'할 일'}))
        await asyncio.sleep(.5);cancel.set()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run());assert not list((tmp_path/'tmp').iterdir())


def test_real_api_pipeline_report_access_and_delete(tmp_path):
    cfg=cli_fixture(tmp_path/'bin');data=tmp_path/'data';data.mkdir()
    (data/'stt-accuracy.json').write_text(json.dumps(asdict(AccuracyConfig(profile='balanced'))))
    app=create_app(data,weather_enabled=False,speech_config=cfg)
    with TestClient(app) as c:
        admin={'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'}
        pair=c.post('/api/devices/pair',headers=admin,json={'name':'test'}).json()
        c.post('/api/devices/claim',headers={'X-Room-Request':'1'},json={'code':pair['path'].split('=')[1]})
        res=c.post('/api/speech/jobs',headers={'X-Room-Request':'1'},data={'request_id':'test1'},files={'file':('a.wav',wav_blob(),'audio/wav')})
        assert res.status_code==202,res.text
        jid=res.json()['id']
        for _ in range(100):
            job=c.get('/api/speech/jobs/'+jid).json()
            if job['status'] not in ['queued','running']:break
            time.sleep(.05)
        assert job['status']=='succeeded',job
        assert 'stt_diagnostics' not in job and 'hint' not in json.dumps(job)
        rich=c.get('/api/speech/jobs/'+jid,headers=admin).json()
        assert rich['stt_diagnostics']['beam_size']==3 and rich['stt_diagnostics']['scores']['available']
        assert rich['stt_diagnostics']['raw_transcript']==job['text']
        assert c.post('/api/speech/jobs',headers={'X-Room-Request':'1'},data={'request_id':'test1'},files={'file':('a.wav',wav_blob(),'audio/wav')}).json()['duplicate']
        assert c.patch('/api/voice/'+job['voice_id'],headers=admin,json={'status':'pending_review','text':'manual edit'}).status_code==200
        rich=c.get('/api/speech/jobs/'+jid,headers=admin).json();assert rich['text']=='manual edit' and rich['stt_diagnostics']['raw_transcript']!='manual edit'
        assert not c.get('/api/state').json()['tasks']
        with app.state.store.connect() as db:
            assert db.execute('select count(*) from llm_requests').fetchone()[0]==0
        assert c.delete('/api/voice/'+job['voice_id'],headers=admin).status_code==200
        with app.state.store.connect() as db:assert db.execute('select count(*) from speech_diagnostics').fetchone()[0]==0


def load_cli():
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'deploy/termux'))
    spec=importlib.util.spec_from_file_location('stt_accuracy_tools',root/'deploy/termux/stt_accuracy_cli.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod


def tool_fixture(tmp_path):
    root=tmp_path/'project';cfg=cli_fixture(root/'runtime/stt/models')
    store=Store(root/'data/room-hub.sqlite3')
    from app.speech import SpeechHub
    async def changed(*a):pass
    SpeechHub(store,root/'data',changed,config=cfg)
    audio=root/'data/audio';audio.mkdir();(audio/'fixture.bin').write_bytes(wav_blob())
    with store.connect() as db:
        db.execute("INSERT INTO voice VALUES('v','r','fixture','audio','old text','ko-KR','pending_review','{}','fixture.bin','audio/wav','d',?)",(utcnow(),))
    (root/'data/speech-config.json').write_text(json.dumps(asdict(cfg)))
    (root/'data/stt-accuracy.json').write_text(json.dumps(asdict(AccuracyConfig(profile='balanced'))))
    (root/'VERSION').write_text('0.1.5')
    sample=root/'runtime/stt/whisper.cpp/samples';sample.mkdir(parents=True);(sample/'jfk.wav').write_bytes(wav_blob())
    return root,cfg,store


def test_profiles_preserve_speech_config(tmp_path):
    mod=load_cli();root,cfg,store=tool_fixture(tmp_path);before=(root/'data/speech-config.json').read_bytes()
    mod.set_profile(root,'legacy',None);assert AccuracyConfig.read(root/'data/stt-accuracy.json').profile=='legacy'
    mod.set_profile(root,'hint','off');assert not AccuracyConfig.read(root/'data/stt-accuracy.json').include_task_titles
    assert (root/'data/speech-config.json').read_bytes()==before
    assert len(list((root/'data').glob('stt-accuracy.before-*')))==2


def test_cli_compare_same_input_and_no_db_or_config_edits(tmp_path,monkeypatch):
    mod=load_cli();root,cfg,store=tool_fixture(tmp_path);monkeypatch.setattr(mod,'BASE_SHA',mod.sha256(Path(cfg.model)))
    monkeypatch.setattr(mod,'port_closed',lambda:None)
    db_before=(root/'data/room-hub.sqlite3').read_bytes();cfg_before=(root/'data/speech-config.json').read_bytes();policy_before=(root/'data/stt-accuracy.json').read_bytes()
    ref=root/'ref.txt';ref.write_text('오늘 남은 할 일 확인해줘')
    mod.compare(root,['legacy','hint','balanced'],1,reference_file=ref)
    assert (root/'data/room-hub.sqlite3').read_bytes()==db_before
    assert (root/'data/speech-config.json').read_bytes()==cfg_before and (root/'data/stt-accuracy.json').read_bytes()==policy_before
    with zipfile.ZipFile(root/'data/stt-accuracy-tests/latest-compare.zip') as z:
        r=json.loads(z.read('report.json'));assert len(r['runs'])==3 and r['runs'][0]['cer']['cer']>0 and r['runs'][2]['cer']['cer']==0
        assert r['source_sha256']==hashlib.sha256(wav_blob()).hexdigest()
        assert set(z.namelist())=={'report.json','report.html'}


def test_actual_process_smoke_fail_never_leaves_stale_pass(tmp_path,monkeypatch):
    mod=load_cli();root,cfg,store=tool_fixture(tmp_path);monkeypatch.setattr(mod,'BASE_SHA',mod.sha256(Path(cfg.model)));monkeypatch.setattr(mod,'port_closed',lambda:None)
    mod.selftest(root);p=root/'data/stt-accuracy-tests/selftest.json';assert json.loads(p.read_text())['ok']
    old=Path(cfg.binary).read_text();Path(cfg.binary).write_text(old.replace("text='ask what your country can do for you'","text='wrong'"))
    with pytest.raises(ValueError):mod.selftest(root)
    assert json.loads(p.read_text())['ok'] is False


def test_html_escape_and_missing_reference():
    mod=load_cli();h=mod.html_report({'runs':[{'profile':'balanced','text':'<script>bad</script>'}]})
    assert '<script>' not in h and '&lt;script&gt;' in h
    assert '정답 없음' in h
