"""Installer failure safety on temporary synthetic source trees; no user data."""
import json,sys,socket
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from apply_all_tasks_update import PATCH_ID,source_digest,digest,plan_update,apply_update,rollback,confirm_stopped,safe_path

@pytest.fixture
def trees(tmp_path):
 p=tmp_path/'patch';t=tmp_path/'target';(p/'files/web').mkdir(parents=True);(t/'app').mkdir(parents=True);(t/'web').mkdir()
 (t/'app/main.py').write_text('old server');(t/'web/client.html').write_text('client');(t/'web/manager.js').write_text('old\n')
 entries=[]
 for rel,content,required in [('web/manager.js',b'new\n',True),('widgets/all-todos/manifest.json',b'{}',False)]:
  dest=p/'files'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content)
  entries.append({'path':rel,'sha256':digest(content),'source_sha256':source_digest(content),'before':[source_digest(b'old\n')] if required else [],'required_existing':required})
 (p/'PATCH_MANIFEST.json').write_text(json.dumps({'patch_id':PATCH_ID,'files':entries}))
 return p,t

def test_apply_idempotent_and_rollback_preserves_private_state(trees):
 p,t=trees;(t/'data').mkdir();(t/'data/private-test.txt').write_text('synthetic-private')
 original=(t/'data/private-test.txt').read_bytes()
 _,plan=plan_update(p,t);assert len(plan)==2
 backup=apply_update(p,t);assert (t/'web/manager.js').read_text()=='new\n'
 assert apply_update(p,t) is None
 assert (t/'data/private-test.txt').read_bytes()==original
 rollback(t,backup);assert (t/'web/manager.js').read_text()=='old\n'
 assert not (t/'widgets/all-todos/manifest.json').exists()
 assert (t/'data/private-test.txt').read_bytes()==original

def test_unknown_local_edit_is_not_overwritten(trees):
 p,t=trees;(t/'web/manager.js').write_text('custom')
 with pytest.raises(ValueError):apply_update(p,t)
 assert (t/'web/manager.js').read_text()=='custom'
 assert not (t/'widgets').exists()

def test_modified_payload_refused(trees):
 p,t=trees;(p/'files/web/manager.js').write_text('damaged')
 with pytest.raises(ValueError):apply_update(p,t)
 assert (t/'web/manager.js').read_text()=='old\n'

def test_crlf_base_is_accepted(trees):
 p,t=trees;(t/'web/manager.js').write_bytes(b'old\r\n');apply_update(p,t)
 assert (t/'web/manager.js').read_bytes()==b'new\n'

def test_symlink_target_refused(trees,tmp_path):
 p,t=trees;(t/'web/manager.js').unlink();other=tmp_path/'else.js';other.write_text('old\n');(t/'web/manager.js').symlink_to(other)
 with pytest.raises(ValueError):apply_update(p,t)
 assert other.read_text()=='old\n'

@pytest.mark.parametrize('relative',['../data/x','data/x','web/../../data/x','web/admin-token.txt','.env','/etc/passwd','web/.env'])
def test_private_or_escaping_path_refused(tmp_path,relative):
 with pytest.raises(ValueError):safe_path(tmp_path,relative)

def test_rollback_refuses_later_edits(trees):
 p,t=trees;b=apply_update(p,t);(t/'web/manager.js').write_text('newer user edit')
 with pytest.raises(ValueError):rollback(t,b)
 assert (t/'web/manager.js').read_text()=='newer user edit'

def test_apply_requires_stop_and_refuses_live_port():
 with pytest.raises(ValueError):confirm_stopped(False,8088)
 with socket.socket() as s:
  s.bind(('127.0.0.1',0));s.listen()
  with pytest.raises(ValueError):confirm_stopped(True,s.getsockname()[1])
