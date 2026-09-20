"""Source updater safety using temporary synthetic trees, never production files."""
from pathlib import Path
import json,socket
import pytest
from scripts import apply_life_update as u

@pytest.fixture
def trees(tmp_path):
    p=tmp_path/'package';t=tmp_path/'target';(t/'app').mkdir(parents=True);(t/'VERSION').write_bytes(b'0.1.6\n');(t/'app/main.py').write_bytes(b'old\n')
    entries=[]
    for name,content in [('app/main.py',b'new\n'),('app/life.py',b'life\n'),('VERSION',b'0.1.7\n')]:
        f=p/'files'/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(content)
        old=t/name;entries.append({'path':name,'before':u.source_digest(old.read_bytes()) if old.exists() else None,'sha256':u.digest(content)})
    (p/'PATCH_MANIFEST.json').write_text(json.dumps({'patch_id':u.PATCH_ID,'from_version':'0.1.6','to_version':'0.1.7','files':entries}))
    return p,t

@pytest.fixture(autouse=True)
def no_custom_data(monkeypatch):monkeypatch.delenv('HUB_DATA_DIR',raising=False)


def test_dry_apply_retry_rollback_private_preserved(trees,monkeypatch):
    p,t=trees;(t/'data').mkdir();(t/'data/speech-config.json').write_text('{"threads":6}')
    (t/'data/room-hub.sqlite3').write_bytes(b'SYNTHETIC PRIVATE DB');(t/'data/admin-token.txt').write_text('SYNTHETIC NOT A TOKEN')
    before={str(f.relative_to(t)):f.read_bytes() for f in (t/'data').iterdir()}
    monkeypatch.setattr(u,'check_stopped',lambda *a:None)
    assert len(u.plan(p,t)[1])==3 and (t/'VERSION').read_text().strip()=='0.1.6'
    backup=u.apply(p,t,stopped=True);assert (t/'VERSION').read_text().strip()=='0.1.7'
    assert u.apply(p,t,stopped=True) is None
    # Runtime may add tables/data before source rollback; do not revert those effects.
    (t/'data/new_notes.bin').write_bytes(b'NEW SYNTHETIC NOTE')
    u.rollback(t,backup,stopped=True);assert (t/'VERSION').read_text().strip()=='0.1.6'
    assert (t/'data/new_notes.bin').read_bytes()==b'NEW SYNTHETIC NOTE'
    assert not (t/'app/life.py').exists()
    assert all((t/name).read_bytes()==data for name,data in before.items())
    u.rollback(t,backup,stopped=True)


def test_requires_stop_and_checks_open_port():
    with pytest.raises(ValueError):u.check_stopped(False,8088)
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));s.listen()
        with pytest.raises(ValueError):u.check_stopped(True,s.getsockname()[1])


def test_unknown_edit_never_overwritten(trees):
    p,t=trees;(t/'app/main.py').write_text('custom')
    with pytest.raises(ValueError):u.plan(p,t)
    assert (t/'app/main.py').read_text()=='custom'


def test_corrupt_payload_and_duplicate_paths(trees):
    p,t=trees;(p/'files/app/life.py').write_text('damaged')
    with pytest.raises(ValueError):u.plan(p,t)
    assert not (t/'app/life.py').exists()


@pytest.mark.parametrize('path',['../data/admin-token.txt','data/admin-token.txt','.env','runtime/model.gguf','/etc/passwd','web/../app/main.py','app\\main.py'])
def test_allowlist(path,tmp_path):
    with pytest.raises(ValueError):u.safe_path(tmp_path,path)


def test_crlf_accepted(trees):
    p,t=trees;(t/'app/main.py').write_bytes(b'old\r\n');assert len(u.plan(p,t)[1])==3


def test_symlink_refused(trees,tmp_path):
    p,t=trees;(t/'app/main.py').unlink();outside=tmp_path/'outside';outside.write_text('old\n')
    try:(t/'app/main.py').symlink_to(outside)
    except (OSError,NotImplementedError):pytest.skip('Symlink creation not permitted')
    with pytest.raises(ValueError):u.plan(p,t)
    assert outside.read_text()=='old\n'


def test_rollback_refuses_later_edits(trees,monkeypatch):
    p,t=trees;monkeypatch.setattr(u,'check_stopped',lambda *a:None);b=u.apply(p,t,stopped=True)
    (t/'app/main.py').write_text('newer user edit')
    with pytest.raises(ValueError):u.rollback(t,b,stopped=True)
    assert (t/'app/main.py').read_text()=='newer user edit'


def test_partial_copy_failure_restores_own_writes(trees,monkeypatch):
    p,t=trees;monkeypatch.setattr(u,'check_stopped',lambda *a:None);real=u.atomic
    def atomic(path,data,mode=0o644):
        if path==t/'app/life.py':raise OSError('synthetic copy failure')
        return real(path,data,mode)
    monkeypatch.setattr(u,'atomic',atomic)
    with pytest.raises(OSError):u.apply(p,t,stopped=True)
    assert (t/'app/main.py').read_bytes()==b'old\n' and not (t/'app/life.py').exists()
    assert (t/'VERSION').read_bytes()==b'0.1.6\n'


def test_mid_apply_external_edit_not_overwritten(trees,monkeypatch):
    p,t=trees;monkeypatch.setattr(u,'check_stopped',lambda *a:None);real=u.atomic
    def atomic(path,data,mode=0o644):
        result=real(path,data,mode)
        if path.name=='receipt.json' and b'installing' in data:(t/'app/main.py').write_text('external')
        return result
    monkeypatch.setattr(u,'atomic',atomic)
    with pytest.raises(ValueError):u.apply(p,t,stopped=True)
    assert (t/'app/main.py').read_text()=='external' and (t/'VERSION').read_bytes()==b'0.1.6\n'
