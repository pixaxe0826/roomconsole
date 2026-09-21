"""Deployment supervisor and BAT contracts; synthetic Git/runit/SSH, never V35."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from test_v35_git_update import make_git_pair, git

ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / 'deploy/windows/V35_Update.bat'


def load(name='remote_update'):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'deploy/termux/{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def remote_script():
    text = BAT.read_text(encoding='utf-8')
    return text.rsplit(':__ROOM_HUB_REMOTE__', 1)[1].lstrip('\n')


def test_bat_preserves_provided_connection_and_safe_ssh():
    text = BAT.read_text(encoding='utf-8')
    for field in ('HOST=192.168.0.14', 'PORT=8022', 'USER=u0_a306'):
        assert field in text
    assert 'ssh.exe -T -p %PORT%' in text
    assert 'StrictHostKeyChecking=no' not in text and 'UserKnownHostsFile=' not in text
    assert 'set "RH_RC=%ERRORLEVEL%"' in text
    assert 'if not "%RH_RC%"=="0" goto :update_failed' in text
    assert 'exit /b 0' in text and 'exit /b 1' in text
    assert 'git -C "$ROOT" show "$TARGET:deploy/termux/update_from_git.py"' in remote_script()
    assert 'git reset' not in remote_script() and 'git pull' not in remote_script()


@pytest.mark.skipif(os.name == 'nt', reason='Bash payload parses on Linux/Termux')
def test_embedded_shell_parses_without_crlf(tmp_path):
    payload = tmp_path / 'remote.sh';payload.write_text(remote_script())
    subprocess.run(['bash', '-n', str(payload)], check=True)


@pytest.mark.parametrize('state,conclusion,expected',[
    ('completed','success','success'), ('completed','failure','failed'),
    ('completed','cancelled','failed'), ('in_progress',None,'pending')])
def test_ci_exact_target(state,conclusion,expected):
    m=load();target='a'*40
    p={'workflow_runs':[{'id':1,'head_sha':target,'head_branch':'main','event':'push',
                        'status':state,'conclusion':conclusion}]}
    assert m.ci_state(p,target)==expected
    assert m.ci_state(p,'b'*40)=='pending'
    p['workflow_runs'][0]['event']='pull_request'
    assert m.ci_state(p,target)=='pending'


def test_ci_latest_run_and_failure_before_deploy(monkeypatch):
    m=load();target='a'*40
    base={'head_sha':target,'head_branch':'main','event':'push','status':'completed'}
    payload={'workflow_runs':[dict(base,id=1,conclusion='success'),dict(base,id=2,conclusion='failure')]}
    assert m.ci_state(payload,target)=='failed'
    monkeypatch.setattr(m,'json_get',lambda _:payload)
    with pytest.raises(m.VerifyError,match='CI failed'):
        m.wait_ci(target,seconds=0)


def test_ci_pending_timeout_has_no_success(monkeypatch):
    m=load();monkeypatch.setattr(m,'json_get',lambda _: {'workflow_runs':[]})
    with pytest.raises(m.VerifyError,match='not successful'):
        m.wait_ci('a'*40,seconds=0)


def setup_verification(tmp_path,monkeypatch,*,status=None,health=None,heads=None):
    m=load();root=tmp_path/'room-hub';root.mkdir();(root/'VERSION').write_text('0.1.7')
    prefix=tmp_path/'prefix';target='a'*40
    count=[0]
    def output(*args):
        if args[0]=='sv':
            count[0]+=1
            if callable(status):return status(str(prefix/'var/service/room-hub'),count[0])
            service = prefix / 'var/service/room-hub'
            return status or f'run: {service}: (pid 123) 30s; run: log: (pid 456) 30s'
        if args[-2]=='rev-parse':
            if heads and args[-1] in heads:return heads[args[-1]]
            return 'b'*40 if 'tree' in args[-1] else target
        if args[-3]=='status':return ''
        raise AssertionError(args)
    monkeypatch.setattr(m,'output',output)
    monkeypatch.setattr(m,'json_get',lambda _:health or {'status':'ok','version':'0.1.7'})
    return m,root,prefix,target


def test_real_success_requires_git_health_and_three_same_pids(tmp_path,monkeypatch):
    m,root,prefix,target=setup_verification(tmp_path,monkeypatch)
    data=m.verify_running(root,prefix,target,delay=0)
    assert data['samples']==3 and data['pid']=='123' and data['source_synced']


@pytest.mark.parametrize('status', ['down: SERVICE: 0s; run: log: (pid 456) 9s', 'finish: SERVICE: (pid 123) 0s'])
def test_log_run_does_not_hide_down_service(tmp_path,monkeypatch,status):
    m,root,prefix,target=setup_verification(tmp_path,monkeypatch,status=status)
    with pytest.raises(m.VerifyError,match='not run'):
        m.verify_running(root,prefix,target,delay=0)


def test_restarting_process_is_not_verified(tmp_path,monkeypatch):
    m,root,prefix,target=setup_verification(tmp_path,monkeypatch,
        status=lambda path,n:f'run: {path}: (pid {100+n}) 0s')
    with pytest.raises(m.VerifyError,match='restarted'):
        m.verify_running(root,prefix,target,delay=0)


@pytest.mark.parametrize('health',[{'status':'bad','version':'0.1.7'},{'status':'ok','version':'0.1.6'}])
def test_bad_health_and_old_version_fail(tmp_path,monkeypatch,health):
    m,root,prefix,target=setup_verification(tmp_path,monkeypatch,health=health)
    with pytest.raises(m.VerifyError,match='Health'):
        m.verify_running(root,prefix,target,delay=0)


def test_wrong_head_never_success(tmp_path,monkeypatch):
    m,root,prefix,target=setup_verification(tmp_path,monkeypatch,heads={'HEAD':'c'*40})
    with pytest.raises(m.VerifyError,match='HEAD/tree'):
        m.verify_running(root,prefix,target,delay=0)


def test_updater_ci_target_mismatch_stops_before_service(tmp_path,monkeypatch):
    m=load('update_from_git');remote,local,env=make_git_pair(tmp_path)
    stopped=[];monkeypatch.setattr(m,'stop_room_hub',lambda *a:stopped.append(True))
    monkeypatch.setenv('ROOM_HUB_EXPECTED_MAIN','f'*40)
    with pytest.raises(m.UpdateError,match='main changed'):
        m.deploy(local,Path(env['PREFIX']),Path(env['HOME']),'origin','main')
    assert not stopped


@pytest.mark.skipif(os.name != 'nt', reason='Actual cmd.exe/PowerShell wrapper tested on Windows CI only')
@pytest.mark.parametrize('ssh_code',[0,23])
def test_windows_cmd_wrapper_with_stub_ssh(tmp_path,ssh_code):
    """Real BAT + PowerShell + TCP loopback. SSH is a compiled test double, no V35."""
    fakebin=tmp_path/'bin';fakebin.mkdir()
    source=tmp_path/'ssh.cs'
    source.write_text('''using System; using System.IO;
class Stub { public static int Main(string[] args) {
 File.WriteAllText(Environment.GetEnvironmentVariable("TEST_PAYLOAD"),Console.In.ReadToEnd());
 File.WriteAllText(Environment.GetEnvironmentVariable("TEST_ARGS"),string.Join("|",args));
 Console.WriteLine("SYNTHETIC SSH TEST DOUBLE");
 return int.Parse(Environment.GetEnvironmentVariable("TEST_SSH_CODE")); }}''')
    subprocess.run(['powershell.exe','-NoProfile','-Command',
        f"Add-Type -Path '{source}' -OutputAssembly '{fakebin/'ssh.exe'}' -OutputType ConsoleApplication"],check=True,capture_output=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen()
        script=tmp_path/'시험 폴더';script.mkdir();bat=script/'V35 update.bat'
        text=BAT.read_text(encoding='utf-8').replace('HOST=192.168.0.14','HOST=127.0.0.1').replace('PORT=8022',f'PORT={listener.getsockname()[1]}')
        bat.write_bytes(text.replace('\n','\r\n').encode('utf-8'))
        env=os.environ.copy();env.update(PATH=str(fakebin)+os.pathsep+env['PATH'],
            TEST_PAYLOAD=str(tmp_path/'payload'),TEST_ARGS=str(tmp_path/'args'),TEST_SSH_CODE=str(ssh_code))
        result=subprocess.run(['cmd.exe','/d','/c',str(bat)],input='\n',text=True,encoding='utf-8',
                              capture_output=True,env=env,timeout=25)
    assert result.returncode==(0 if ssh_code==0 else 1),result.stdout+result.stderr
    assert ('[SUCCESS]' in result.stdout)==(ssh_code==0)
    data=(tmp_path/'payload').read_bytes()
    assert data.decode('utf-8')==remote_script() and b'\r' not in data and not data.startswith(b'\xef\xbb\xbf')
    args=(tmp_path/'args').read_text()
    assert '-T|' in args and 'u0_a306@127.0.0.1|bash -s' in args


@pytest.mark.skipif(os.name == 'nt', reason='Termux supervisor uses fcntl; wrapper is tested separately on Windows')
@pytest.mark.parametrize('failed_step',['ci','updater','verify','none'])
def test_supervisor_failure_never_prints_verified_success(tmp_path,monkeypatch,capsys,failed_step):
    m=load();root=tmp_path/'room-hub';root.mkdir()
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('PREFIX',str(tmp_path/'prefix'))
    called=[]
    def ci(*_):
        called.append('ci')
        if failed_step=='ci':raise m.VerifyError('synthetic CI failure')
    def update(*_,**kw):
        called.append('updater')
        if failed_step=='updater':raise subprocess.CalledProcessError(1,'synthetic updater')
    def verify(*_):
        called.append('verify')
        if failed_step=='verify':raise m.VerifyError('synthetic down service')
        return {'head':'a'*40,'tree':'b'*40,'pid':'123'}
    monkeypatch.setattr(m,'validate_source',lambda *_:None)
    monkeypatch.setattr(m,'wait_ci',ci);monkeypatch.setattr(m.subprocess,'run',update)
    monkeypatch.setattr(m,'verify_running',verify)
    rc=m.main(['--root',str(root),'--target','a'*40]);out=capsys.readouterr()
    assert rc==(0 if failed_step=='none' else 1)
    assert ('VERIFIED UPDATE COMPLETE' in out.out)==(failed_step=='none')
    if failed_step=='ci':assert called==['ci']
    elif failed_step=='updater':assert called==['ci','updater']
