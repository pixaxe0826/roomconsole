"""External loader/transfer and PR #20 tests: generated data only, no real V35."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from benchmark_cases import make_suite
from benchmarks.dataset import load_directory, load_suite, stable, list_suites
from benchmarks.integrity import RECEIPT, sha256_file
from benchmarks.paths import CODE_ROOT
from benchmarks.runtime import Runtime, TextInput
from benchmarks.transfer import prepare, receive_init, install, unpack, discover_source


def test_no_default_dataset_or_missing_root(monkeypatch, tmp_path):
    monkeypatch.delenv('ROOM_HUB_BENCHMARK_DATA', raising=False)
    with pytest.raises(ValueError, match='no dataset is bundled'): list_suites()
    with pytest.raises(ValueError, match='does not exist'): list_suites(tmp_path/'missing')
    make_suite(tmp_path)
    monkeypatch.setenv('ROOM_HUB_BENCHMARK_DATA', str(tmp_path))
    assert list_suites() == ['synthetic']
    assert len(load_suite('synthetic').cases) == 4


@pytest.mark.parametrize('which', ['repo', 'git', 'symlink', 'manifest-symlink'])
def test_external_boundaries(tmp_path, which):
    if which == 'repo':
        with pytest.raises(ValueError, match='outside'): list_suites(CODE_ROOT)
        return
    p = make_suite(tmp_path)
    if which == 'git':
        (tmp_path/'.git').mkdir()
    else:
        source, dest = (p, tmp_path/'alias') if which == 'symlink' else (p/'schema.json', p/'temp.json')
        if which == 'manifest-symlink':
            source.rename(dest)
            source, dest = dest, source
        try: dest.symlink_to(source, target_is_directory=source.is_dir())
        except OSError: pytest.skip('Host cannot create symlinks')
        if which == 'symlink': p = dest
    with pytest.raises(ValueError): load_directory(p, name='synthetic')


def test_source_leaf_parent_and_ambiguous(tmp_path):
    source=tmp_path/'source'; p=make_suite(source)
    assert discover_source(p)==p
    assert discover_source(source)==p
    make_suite(source,'another')
    with pytest.raises(ValueError,match='one suite'): discover_source(source)
    assert discover_source(source,'synthetic')==p


def packaged(tmp_path):
    source=make_suite(tmp_path/'local folder with spaces')
    before={p.name:p.read_bytes() for p in source.iterdir()}
    meta=prepare(source,tmp_path/'package with spaces')
    assert before=={p.name:p.read_bytes() for p in source.iterdir()}
    root=tmp_path/'v35-data';root.mkdir()
    token='a'*32; stage=receive_init(root,token)
    shutil.copyfile(meta['archive'],stage/'bundle.zip')
    return root,token,meta


def test_package_install_integrity_and_explicit_overwrite(tmp_path):
    root,token,meta=packaged(tmp_path)
    got=install(root,token,meta['archive_sha256'])
    final=root/'synthetic'
    assert got['dataset_hash']==meta['dataset_hash']==load_suite('synthetic',root).digest
    assert not (final/'synthetic').exists() and not (root/'.incoming'/token).exists()
    stage=receive_init(root,token);shutil.copyfile(meta['archive'],stage/'bundle.zip')
    before={p.name:p.read_bytes() for p in final.iterdir()}
    with pytest.raises(FileExistsError):install(root,token,meta['archive_sha256'])
    assert before=={p.name:p.read_bytes() for p in final.iterdir()}
    replaced=install(root,token,meta['archive_sha256'],force=True)
    assert Path(replaced['backup']).is_dir() and load_suite('synthetic',root).digest==meta['dataset_hash']


def test_partial_transfer_never_promoted(tmp_path):
    root,token,meta=packaged(tmp_path)
    (root/'.incoming'/token/'bundle.zip').write_bytes(b'partial')
    with pytest.raises(ValueError,match='SHA256'): install(root,token,meta['archive_sha256'])
    assert not (root/'synthetic').exists() and list_suites(root)==[]


def test_data_change_or_added_file_after_transfer_rejected(tmp_path):
    root,token,meta=packaged(tmp_path);install(root,token,meta['archive_sha256'])
    target=root/'synthetic'/'schema.json'; original=target.read_bytes()
    target.write_bytes(original+b' ')
    with pytest.raises(ValueError,match='checksum'):load_suite('synthetic',root)
    target.write_bytes(original);(target.parent/'unlisted.txt').write_text('unexpected')
    with pytest.raises(ValueError,match='unlisted'):load_suite('synthetic',root)


@pytest.mark.parametrize('badname', ['../escape.json','/absolute.json','files/../x.json','files/C:x.json','files/back\\slash.json','files/a\nb.json'])
def test_unsafe_archive_entries_rejected(tmp_path,badname):
    zpath=tmp_path/'bad.zip'
    with zipfile.ZipFile(zpath,'w') as z:z.writestr(badname,'bad')
    dest=tmp_path/'unpacked';dest.mkdir()
    with pytest.raises(ValueError):unpack(zpath,dest,sha256_file(zpath))
    assert list(dest.iterdir())==[]


def test_archive_symlink_rejected(tmp_path):
    zpath=tmp_path/'bad.zip'
    with zipfile.ZipFile(zpath,'w') as z:
        i=zipfile.ZipInfo('files/link.json');i.create_system=3;i.external_attr=0o120777<<16
        z.writestr(i,'/outside')
    dest=tmp_path/'unpacked';dest.mkdir()
    with pytest.raises(ValueError,match='regular'):unpack(zpath,dest,sha256_file(zpath))


def test_import_lock_prevents_concurrent_promote(tmp_path):
    root,token,meta=packaged(tmp_path);(root/'.import-lock').mkdir()
    with pytest.raises(ValueError,match='import'):install(root,token,meta['archive_sha256'])
    assert not (root/'synthetic').exists()


def test_changed_source_is_not_silently_packaged(tmp_path,monkeypatch):
    import benchmarks.transfer as transfer
    src=make_suite(tmp_path/'src');real=transfer.describe
    def mutate(*args):
        values=real(*args);(src/'fixtures.json').write_text('{}');return values
    monkeypatch.setattr(transfer,'describe',mutate)
    with pytest.raises(ValueError,match='Source changed'):prepare(src,tmp_path/'output')
    assert not (tmp_path/'output').exists()


def test_json_array_and_legacy_manifest_fallback(tmp_path):
    src=make_suite(tmp_path);manifest=json.loads((src/'manifest.json').read_text())
    rows=[json.loads(s) for s in (src/'cases.jsonl').read_text().splitlines()]
    (src/'cases.json').write_text(stable(rows),encoding='utf-8');manifest['case_files']=['cases.json']
    (src/'manifest.json').write_text(stable(manifest),encoding='utf-8')
    assert len(load_suite('synthetic',tmp_path).cases)==4
    (src/'cases.json').unlink();(src/'cases.jsonl').rename(src/'all_250.jsonl')
    manifest.pop('case_files');(src/'manifest.json').write_text(stable(manifest),encoding='utf-8')
    assert len(load_suite('synthetic',tmp_path).cases)==4  # no hardcoded 250-case dependency


def test_cli_external_smoke_limit_and_provenance(tmp_path):
    src=make_suite(tmp_path/'datasets');results=tmp_path/'results'
    base=[sys.executable,'-m','benchmarks','--suite-root',str(src.parent),'--results-root',str(results)]
    p=subprocess.run(base+['validate','--suite','synthetic'],cwd=CODE_ROOT,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    p=subprocess.run(base+['run','--suite','synthetic','--name','smoke','--limit','2'],cwd=CODE_ROOT,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    cfg=json.loads((results/'smoke'/'config.json').read_text())
    assert cfg['case_count']==4 and cfg['selected_count']==2 and cfg['dataset_source']=='external'
    assert 'production_git_sha' in cfg and 'benchmark_git_sha' in cfg
    assert cfg['dataset_hash']==load_suite('synthetic',src.parent).digest
    before={p.name:p.read_bytes() for p in src.iterdir()}
    p=subprocess.run(base+['run','--suite','synthetic','--name','smoke'],cwd=CODE_ROOT,capture_output=True,text=True)
    assert p.returncode==2 and before=={p.name:p.read_bytes() for p in src.iterdir()}


def timer_runtime(tmp_path, *, timers=(), layout=None):
    src=make_suite(tmp_path)
    fixture=json.loads((src/'fixtures.json').read_text())
    fixture['timers']=list(timers)
    if layout is not None:fixture['layout']=layout
    return Runtime(fixture,'2026-09-21T09:00:00+09:00','Asia/Seoul',{'llm':'disabled'})


def test_pr20_fake_adapter_independent_slots_and_current(tmp_path):
    async def run():
        r=timer_runtime(tmp_path)
        try:
            req=lambda text:TextInput(text,r.clock.iso(),{})
            a=await r.run(req('4분 타이머 시작'));b=await r.run(req('1분 30초 타이머 시작'))
            left=a['adapter_calls'][0]['result'];right=b['adapter_calls'][0]['result']
            assert left['widget_id']=='test-timer-a' and right['widget_id']=='test-timer-b'
            assert left['duration_seconds']==240 and right['duration_seconds']==90
            blocked=await r.run(req('2분 타이머 시작'))
            assert not blocked['business_state_changed'] and r.app.state.timers.snapshot()['active_count']==2
            stop=await r.run(req('현재 타이머 종료'))
            assert stop['adapter_calls'][0]['result']['id']==right['id']
            assert r.app.state.timers.get(left['id'])['state']=='running'
            third=await r.run(req('3분 타이머 시작'))
            assert third['adapter_calls'][0]['result']['widget_id']=='test-timer-b'
            assert third['adapter_calls'][0]['adapter']=='FakeTimerAdapter'
        finally:await r.close()
    asyncio.run(run())


def test_pr20_scoped_ownership_and_receipts_are_production_behavior(tmp_path):
    from app.timers import TimerStart,TimerStop
    from fastapi import HTTPException
    async def run():
        r=timer_runtime(tmp_path)
        try:
            s=r.app.state.timers
            a=s.start(TimerStart(widget_id='test-timer-a',duration_seconds=240,request_id='a'),'test')
            b=s.start(TimerStart(widget_id='test-timer-b',duration_seconds=90,request_id='b'),'test')
            with pytest.raises(HTTPException):s.stop(b['id'],TimerStop(widget_id='test-timer-a',request_id='wrong'),'test')
            stopped=s.stop('current',TimerStop(widget_id='test-timer-a',request_id='stop-a'),'test')
            assert stopped['id']==a['id'] and s.get(b['id'])['state']=='running'
            newer=s.start(TimerStart(widget_id='test-timer-a',duration_seconds=60,request_id='new-a'),'test')
            replay=s.stop('current',TimerStop(widget_id='test-timer-a',request_id='stop-a'),'test')
            assert replay['duplicate'] and s.get(newer['id'])['state']=='running'
        finally:await r.close()
    asyncio.run(run())


def test_pr20_legacy_unbound_fixture_reconciles_without_reset(tmp_path):
    async def run():
        rows=[dict(id=f'legacy-{n}',duration_seconds=240,started_at='2026-09-21T08:59:00+09:00') for n in range(3)]
        r=timer_runtime(tmp_path,timers=rows)
        try:
            s=r.app.state.timers
            assert s.get('legacy-0')['widget_id']=='test-timer-a'
            assert s.get('legacy-1')['widget_id']=='test-timer-b'
            assert s.get('legacy-2')['widget_id'] is None
            assert all(x['remaining_seconds']==180 for x in s.snapshot()['items'])
            assert s.snapshot()['current_id']=='legacy-2'
        finally:await r.close()
    asyncio.run(run())


def test_invalid_fixture_constructor_restores_sqlite_factory(tmp_path):
    import sqlite3
    original=sqlite3.connect
    src=make_suite(tmp_path);f=json.loads((src/'fixtures.json').read_text())
    f['layout']['widgets'][1]['id']=f['layout']['widgets'][2]['id']
    with pytest.raises(ValueError):Runtime(f,'2026-09-21T09:00:00+09:00','Asia/Seoul',{'llm':'disabled'})
    assert sqlite3.connect is original


def test_policy_blocks_suites_but_not_widget_manifests():
    from scripts.check_benchmark_policy import violations
    assert violations(['benchmarks/suites/x/manifest.json','data-notes.md'])==['benchmarks/suites/x/manifest.json']
    assert not violations(['widgets/timers/manifest.json','tests/benchmark_cases.py'])
