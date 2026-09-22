"""Wrapper syntax/native argv tests. Never connect to the user's Windows/V35."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from benchmark_cases import make_suite

ROOT=Path(__file__).resolve().parents[1]


def test_powershell_syntax_argv_and_orchestration(tmp_path):
    shell=shutil.which('pwsh') or shutil.which('powershell')
    if not shell and os.name == 'nt':
        pytest.fail('PowerShell is required for Windows transport CI')
    if not shell:
        pytest.skip('PowerShell unavailable locally; required by Windows CI')
    source=make_suite(tmp_path/'local dataset folder')
    proc=subprocess.run([shell,'-NoProfile','-NonInteractive','-File',str(ROOT/'scripts/test_benchmark_transport.ps1'),
                         '-PythonExe',sys.executable,'-SourceRoot',str(source)],cwd=ROOT,capture_output=True,text=True)
    assert proc.returncode==0,proc.stdout+'\n'+proc.stderr
    assert 'POWERSHELL_TRANSPORT_TESTS_PASSED' in proc.stdout


@pytest.mark.skipif(os.name=='nt',reason='Outer Termux wrappers are Bash; tested on Linux CI')
def test_wrapper_binds_external_dataset_and_allows_compare_without_data(tmp_path):
    repo=tmp_path/'room-hub';d=repo/'deploy/termux';d.mkdir(parents=True)
    for name in ('benchmark.sh','benchmark-data.sh'):shutil.copyfile(ROOT/'deploy/termux'/name,d/name)
    (repo/'.venv-v35').mkdir();(repo/'.venv-v35/pyvenv.cfg').touch()
    prefix=tmp_path/'prefix';(prefix/'bin').mkdir(parents=True)
    fake=prefix/'bin/proot-distro'
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n');fake.chmod(0o700)
    home=tmp_path/'home';home.mkdir();data=home/'room-hub-benchmark-data'
    env={**os.environ,'HOME':str(home),'PREFIX':str(prefix)}
    env.pop('ROOM_HUB_BENCHMARK_DATA',None)
    def run(*args):return subprocess.run(['bash',str(d/'benchmark.sh'),*args],env=env,capture_output=True,text=True)
    assert run('validate','--suite','synthetic').returncode!=0
    data.mkdir()
    p=run('validate','--suite','synthetic');assert p.returncode==0,p.stderr
    assert str(data)+':/opt/benchmark-data' in p.stdout
    assert '--suite-root\n/opt/benchmark-data' in p.stdout
    assert ':ro' not in p.stdout  # PRoot is path translation, not a read-only mount.
    assert run('--suite-root','/arbitrary','validate','--suite','synthetic').returncode!=0
    assert run('--data-root',str(repo),'list').returncode!=0
    (data/'.import-lock').mkdir()
    assert run('list').returncode!=0
    (data/'.import-lock').rmdir();data.rmdir()
    p=run('compare','before','after');assert p.returncode==0
    assert '/opt/benchmark-data' not in p.stdout
    p=subprocess.run(['bash',str(d/'benchmark-data.sh'),'prepare','a'*32],env=env,capture_output=True,text=True)
    assert p.returncode==0 and 'receive-init' in p.stdout and data.is_dir()
    p=subprocess.run(['bash',str(d/'benchmark-data.sh'),'install','bad;token','f'*64],env=env,capture_output=True,text=True)
    assert p.returncode!=0
    for name in ('benchmark.sh','benchmark-data.sh'):
        assert subprocess.run(['bash','-n',str(d/name)],capture_output=True).returncode==0
