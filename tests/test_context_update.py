"""Synthetic filesystem tests, no actual user settings or services."""
import json,sys,hashlib,socket
from pathlib import Path
import pytest
from scripts import apply_context_update as u

@pytest.fixture
def sample(tmp_path,monkeypatch):
 monkeypatch.delenv('HUB_DATA_DIR',raising=False)
 root=tmp_path/'room-hub';package=tmp_path/'patch'
 for rel,content in {'VERSION':'0.1.5\n','app/main.py':'# baseline\n','web/client.html':'<!doctype html>','app/speech.py':'# untouched STT\n'}.items():
  path=root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
 (root/'data').mkdir()
 (root/'data/speech-config.json').write_text(json.dumps({'model':'/opt/room-hub/runtime/stt/models/ggml-base.bin','language':'ko','threads':6}))
 (root/'data/stt-accuracy.json').write_text('{"schema":1,"profile":"careful","include_task_titles":true,"max_task_titles":4}')
 (root/'data/private.txt').write_text('not-a-real-secret')
 entries=[]
 for rel,new in {'VERSION':b'0.1.6\n','app/main.py':b'# updated\n','app/clock_service.py':b'# new\n'}.items():
  dest=package/'files'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(new)
  old=root/rel
  entries.append({'path':rel,'sha256':u.digest(new),'source_sha256':u.source_digest(new),'before':[u.source_digest(old.read_bytes())] if old.exists() else [],'required_existing':old.exists()})
 (package/'PATCH_MANIFEST.json').write_text(json.dumps({'patch_id':u.PATCH_ID,'files':entries}))
 return root,package

def test_stt_and_app_preserved_with_rollback(sample):
 root,pkg=sample;state={p.name:p.read_bytes() for p in (root/'data').iterdir()}
 assert u.speech_guard(root)['beam']==5
 backup=u.apply_update(pkg,root)
 assert (root/'VERSION').read_text().strip()=='0.1.6'
 assert u.apply_update(pkg,root) is None
 assert state=={p.name:p.read_bytes() for p in (root/'data').iterdir()}
 assert (root/'app/speech.py').read_text()=='# untouched STT\n'
 u.rollback(root,backup)
 assert (root/'VERSION').read_text().strip()=='0.1.5' and not (root/'app/clock_service.py').exists()
 assert state=={p.name:p.read_bytes() for p in (root/'data').iterdir()}

@pytest.mark.parametrize('setting,value',[('threads',8),('language','en'),('model','ggml-small.bin')])
def test_reject_wrong_stt_without_changes(sample,setting,value):
 root,pkg=sample;f=root/'data/speech-config.json';cfg=json.loads(f.read_text());cfg[setting]=value;f.write_text(json.dumps(cfg))
 with pytest.raises(ValueError,match='Expected existing Whisper'):u.plan_update(pkg,root)
 assert (root/'VERSION').read_text().strip()=='0.1.5'

def test_wrong_beam_rejected(sample):
 root,pkg=sample;(root/'data/stt-accuracy.json').write_text('{"profile":"balanced"}')
 with pytest.raises(ValueError):u.plan_update(pkg,root)

def test_source_only_checkout_explicit(sample):
 root,pkg=sample
 for p in (root/'data').glob('*.json'):p.unlink()
 assert u.speech_guard(root)['checked'] is False
 assert u.plan_update(pkg,root)[1]

def test_partial_stt_missing_rejected(sample):
 root,pkg=sample;(root/'data/stt-accuracy.json').unlink()
 with pytest.raises(ValueError):u.plan_update(pkg,root)

@pytest.mark.parametrize('path',['data/secret','runtime/qwen/model.gguf','../data/db','/etc/passwd','app/speech.py','app/stt_accuracy.py','web/client.js'])
def test_protected_paths(sample,path):
 with pytest.raises(ValueError):u.safe_path(sample[0],path)

def test_live_listener_and_flag_required(sample):
 with socket.socket() as s:
  s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1]
  with pytest.raises(ValueError):u.confirm_stopped(True,port)
  with pytest.raises(ValueError):u.confirm_stopped(False,port)

def test_unknown_local_change_rejected(sample):
 root,pkg=sample;(root/'app/main.py').write_text('# private fork')
 with pytest.raises(ValueError):u.apply_update(pkg,root)
 assert (root/'VERSION').read_text().strip()=='0.1.5'

def test_crlf_accepted_and_restored(sample):
 root,pkg=sample;(root/'app/main.py').write_bytes(b'# baseline\r\n')
 backup=u.apply_update(pkg,root);u.rollback(root,backup)
 assert (root/'app/main.py').read_bytes()==b'# baseline\r\n'

def test_payload_tampering_refused(sample):
 root,pkg=sample;(pkg/'files/app/main.py').write_text('# tamper')
 with pytest.raises(ValueError):u.plan_update(pkg,root)

def test_symlink_target_refused(sample):
 root,pkg=sample;(root/'app/main.py').unlink();(root/'app/main.py').symlink_to(root/'app/speech.py')
 with pytest.raises(ValueError):u.plan_update(pkg,root)

def test_config_hash_tracks_nonperformance_values(sample):
 root,pkg=sample;before=u.speech_guard(root)
 f=root/'data/stt-accuracy.json';v=json.loads(f.read_text());v['include_task_titles']=False;f.write_text(json.dumps(v))
 assert u.speech_guard(root)!=before
