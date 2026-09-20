#!/usr/bin/env python3
"""Room Hub Qwen runtime. Standard library only; no inference simulation path.

A verified GGUF and on-device smoke pass are required before service startup.
No code path interprets an LLM output as a shell command or executes Room Hub tools.
"""
from __future__ import annotations
import argparse, contextlib, datetime, fcntl, hashlib, json, math, os
from pathlib import Path
import re, shutil, signal, socket, struct, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request

PACKAGE = 'room-hub-0.1.4-qwen-runtime1'
MODEL = 'Qwen3-0.6B-Q5_K_M.gguf'
MODEL_SHA = '925f6b806ce5686701d8b903299d398b8ece537d7ed03253d374e1eeadb585a6'
REVISION = 'ef4088322893040952513f532f736ddeab518403'
MODEL_URL = f'https://huggingface.co/Qwen/Qwen3-0.6B-GGUF/resolve/{REVISION}/{MODEL}'
COMMIT = '4762ad7316dcdec20016ab5985fb46a27902204d'
MODEL_MIN = 500_000_000
MODEL_MAX = 600_000_000
HOST = '127.0.0.1'
PORT = 8090
ROOT = Path(__file__).resolve().parents[3]
MAX_HTTP = 512 * 1024

class Stop(RuntimeError): pass

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()
def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.is_symlink(): raise Stop('Refusing symlink destination: '+str(path))
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o600)
            json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
def read_json(path):
    p=Path(path)
    if p.is_symlink():raise Stop('Refusing symlink: '+str(p))
    return json.loads(p.read_text('utf-8'))
def state_dir(root):return Path(root)/'data/qwen-runtime'
def runtime_dir(root):return Path(root)/'runtime/qwen'
def binary_path(root):return runtime_dir(root)/'build/bin/llama-server'
def model_path(root):return runtime_dir(root)/'models'/MODEL

def speech_guard(root):
    p=Path(root)/'data/speech-config.json'
    cfg=read_json(p)
    if (cfg.get('enabled') is not True or cfg.get('threads')!=6 or cfg.get('language')!='ko'
            or Path(cfg.get('model','')).name!='ggml-base.bin'):
        raise Stop('Expected working Whisper Base / 6 threads / ko. No speech settings were changed.')
    return digest(p)

@contextlib.contextmanager
def lock(root,name):
    p=state_dir(root)/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if p.is_symlink():raise Stop('Refusing symlink lock')
    with p.open('a') as f:
        os.chmod(p,0o600)
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Stop('Another Qwen operation is already running: '+name)
        yield

def assert_free(port=PORT):
    with socket.socket() as s:
        try:s.bind((HOST,port))
        except OSError:raise Stop(f'Port {port} is occupied. Do not stop unrelated services; inspect first.')

def validate_model(path, expected=MODEL_SHA, minimum=MODEL_MIN, maximum=MODEL_MAX):
    p=Path(path)
    if p.is_symlink() or not p.is_file():raise Stop('Missing or unsafe model file')
    if not minimum<=p.stat().st_size<=maximum:raise Stop('GGUF file size outside the expected range')
    with p.open('rb') as f:header=f.read(8)
    if len(header)!=8 or header[:4]!=b'GGUF' or struct.unpack('<I',header[4:])[0] not in (2,3):
        raise Stop('Not a supported GGUF header')
    got=digest(p)
    if got!=expected:raise Stop('Model SHA256 mismatch. Active services were not changed.')
    return {'name':MODEL,'sha256':got,'bytes':p.stat().st_size}

class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urllib.parse.urlsplit(newurl).scheme!='https':raise Stop('Refusing insecure model redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def download_model(root):
    dest=model_path(root);dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if dest.exists() or dest.is_symlink():
        result=validate_model(dest);print('MODEL ALREADY VERIFIED:',MODEL);return result
    part=dest.with_suffix(dest.suffix+'.part')
    if part.is_symlink():raise Stop('Refusing symlink partial download')
    # Retry starts a fresh partial file. A partial file is never used for inference.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),HTTPSRedirect())
    for attempt in range(1,4):
        try:
            deadline=time.monotonic()+3600;received=0;notice=0
            req=urllib.request.Request(MODEL_URL,headers={'User-Agent':PACKAGE,'Accept-Encoding':'identity'})
            with opener.open(req,timeout=45) as r,part.open('wb') as f:
                os.chmod(part,0o600)
                if urllib.parse.urlsplit(r.geturl()).scheme!='https':raise Stop('Insecure model response')
                while True:
                    if time.monotonic()>deadline:raise Stop('Download exceeded one hour')
                    b=r.read(1024*1024)
                    if not b:break
                    received+=len(b)
                    if received>MODEL_MAX:raise Stop('Download exceeds expected model size')
                    f.write(b)
                    if received-notice>=32*1024*1024:
                        print(f'Model downloaded: {received/1048576:.0f} MiB',flush=True);notice=received
                f.flush();os.fsync(f.fileno())
            result=validate_model(part);os.replace(part,dest)
            print('MODEL VERIFIED:',MODEL);return result
        except (OSError,urllib.error.URLError,Stop) as e:
            # Do not expose transient signed download URLs from exception strings.
            print(f'Download/check attempt {attempt}/3 failed ({type(e).__name__}).',flush=True)
            if attempt==3:raise Stop('Model download or SHA256 check failed. Retry setup; TLS checks were not disabled.')
            time.sleep(2*attempt)

# No host, model, thread or GPU selection is accepted from incoming requests here.
def server_args(root,port=PORT):
    return [str(binary_path(root)), '-m',str(model_path(root)), '--alias',MODEL,
            '--host',HOST,'--port',str(port),'-t','6','-tb','6','-c','1024','-np','1',
            '-b','128','-ub','128','-n','256','--device','none','-ngl','0',
            '--jinja','--chat-template-kwargs','{"enable_thinking":false}',
            '--reasoning-budget','0','--no-context-shift','--poll','0','--poll-batch','0',
            '--no-webui','--no-slots','--threads-http','2',
            '--temp','0.2','--top-k','20','--top-p','0.8','--min-p','0','--verbosity','2']

def clean_env():
    # An unrelated shell's llama flags must not override this private service.
    return {k:v for k,v in os.environ.items()
            if not (k.startswith('LLAMA_') or k.startswith('GGML_') or k in ('HF_TOKEN','HUB_ADMIN_TOKEN','HUB_INGEST_TOKEN'))}

def http_json(port,path,body=None,method=None,headers=None,timeout=30):
    if not path.startswith('/') or '\r' in path or '\n' in path:raise Stop('Invalid local API path')
    data=None if body is None else json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
    hs={'Content-Type':'application/json','X-Room-Request':'1'};hs.update(headers or {})
    req=urllib.request.Request(f'http://127.0.0.1:{port}{path}',data=data,headers=hs,method=method)
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with op.open(req,timeout=timeout) as r:
            raw=r.read(MAX_HTTP+1)
            if len(raw)>MAX_HTTP:raise Stop('Local API response too large')
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        # Avoid disclosing admin token or original transcripts in logs.
        raise Stop(f'Local API HTTP {e.code}: {path}. Inspect Manager; no retries of inference are automatic.') from None
    except (OSError,ValueError) as e:
        raise Stop(f'Local API unavailable or invalid JSON: {path} ({type(e).__name__})') from None

def inspect_backend(port=PORT):
    result=http_json(port,'/v1/models')
    if not any(d.get('id')==MODEL for d in result.get('data',[]) if isinstance(d,dict)):
        raise Stop('The local model ID does not match the selected Qwen GGUF')
    props=http_json(port,'/props')
    ctx=props.get('default_generation_settings',{}).get('n_ctx')
    if ctx!=1024 or props.get('total_slots')!=1:
        raise Stop('Expected context=1024 and one inference slot')
    return {'model':MODEL,'context':ctx,'parallel':1,'build_info':props.get('build_info')}

def validate_answer(result):
    try:
        c=result['choices'][0];m=c['message'];text=m['content'];u=result['usage'];tm=result['timings']
        if not isinstance(text,str) or not text.strip():raise ValueError()
        if c.get('finish_reason')!='stop':raise ValueError()
        if m.get('reasoning_content') or m.get('reasoning') or '<think>' in text or '</think>' in text:raise ValueError()
        if not any('\uac00'<=ch<='\ud7a3' for ch in text):raise ValueError()
        for key in ('prompt_tokens','completion_tokens'):
            if type(u.get(key)) is not int or u[key]<=0:raise ValueError()
        for key in ('predicted_ms','predicted_per_second'):
            if type(tm.get(key)) not in (float,int) or not math.isfinite(tm[key]) or tm[key]<=0:raise ValueError()
        if type(tm.get('predicted_n')) is not int or tm['predicted_n']<=0:raise ValueError()
    except (KeyError,IndexError,TypeError,ValueError):
        raise Stop('Actual model smoke failed: require completed Korean text, no thinking, usage and generation timings') from None
    return {'text':text,'finish_reason':c['finish_reason'],'usage':u,'timings':tm}

def kill_group(proc,grace=8):
    # This function is only used for Popen(start_new_session=True) created here.
    with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGTERM)
    try:proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
        proc.wait(timeout=5)
    # Include children left behind if the session leader has already exited.
    with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)

def run_smoke(root):
    root=Path(root);sd=state_dir(root);sd.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Remove stale success first: a failed test must never certify a new binary.
    (sd/'ready.json').unlink(missing_ok=True)
    model=validate_model(model_path(root));binary=binary_path(root)
    if not binary.is_file() or not os.access(binary,os.X_OK):raise Stop('llama-server binary is missing')
    build=read_json(runtime_dir(root)/'build-receipt.json')
    if build.get('commit')!=COMMIT or build.get('binary_sha256')!=digest(binary):raise Stop('Unverified runtime build')
    before=speech_guard(root);report={'ok':False,'kind':'actual-llama-server','at':now(),'cases':[]}
    # Dynamic loopback port avoids interfering with all production ports.
    with socket.socket() as sock:sock.bind((HOST,0));port=sock.getsockname()[1]
    log=sd/'smoke-engine.log'
    try:
        with log.open('w') as out:
            os.chmod(log,0o600)
            proc=subprocess.Popen(server_args(root,port),stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,
                                  start_new_session=True,env=clean_env())
            try:
                deadline=time.monotonic()+300
                while True:
                    if proc.poll() is not None:raise Stop('llama-server exited during initialization. See private smoke-engine.log')
                    try:
                        if http_json(port,'/health',timeout=2).get('status')=='ok':break
                    except Stop:pass
                    if time.monotonic()>deadline:raise Stop('Model initialization exceeded 300 seconds')
                    time.sleep(1)
                report['backend']=inspect_backend(port)
                for prompt in ('한국어로 짧게 인사해 주세요.','다음 문장에서 할 일 제목만 한 줄로 답하세요: 내일 택배 보내기.'):
                    payload={'model':MODEL,'messages':[{'role':'system','content':'한국어로 짧게 답하세요. 실제 작업은 실행하지 않습니다.'},
                              {'role':'user','content':prompt}], 'stream':False,'max_tokens':96,'temperature':0.2,
                              'chat_template_kwargs':{'enable_thinking':False}}
                    start=time.monotonic();result=http_json(port,'/v1/chat/completions',payload,timeout=300)
                    checked=validate_answer(result);checked.update(input=prompt,request_seconds=time.monotonic()-start)
                    report['cases'].append(checked)
            finally:kill_group(proc)
        if digest(root/'data/speech-config.json')!=before:raise Stop('Speech settings changed during test. Inspect before enabling.')
        report.update(ok=True,model=model,binary_sha256=digest(binary),commit=COMMIT,
                      package=PACKAGE,threads=6,context=1024,non_thinking=True,cpu_only=True,
                      code_sha256=digest(Path(__file__)),api_port=PORT)
        atomic_json(sd/'ready.json',report)
        print('QWEN READY: actual engine returned Korean output, usage and timings; Room Hub dispatch remains unchanged.')
        return report
    finally:atomic_json(sd/'last-smoke.json',report)

def validate_ready(root):
    r=read_json(state_dir(root)/'ready.json')
    if (r.get('ok') is not True or r.get('kind')!='actual-llama-server' or r.get('package')!=PACKAGE
            or r.get('commit')!=COMMIT or r.get('threads')!=6 or r.get('context')!=1024
            or r.get('code_sha256')!=digest(Path(__file__))):raise Stop('Successful on-device setup/smoke receipt required')
    validate_model(model_path(root))
    if digest(binary_path(root))!=r.get('binary_sha256'):raise Stop('Binary changed after smoke. Run setup again.')
    return r

def serve(root):
    root=Path(root);sd=state_dir(root);request=sd/'stop.request'
    with lock(root,'server.lock'):
        validate_ready(root);assert_free()
        before=request.read_bytes() if request.exists() else None
        current=before
        ending=False
        def halt(*_):
            nonlocal ending
            ending=True
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,halt)
        proc=subprocess.Popen(server_args(root),stdin=subprocess.DEVNULL,start_new_session=True,env=clean_env())
        atomic_json(sd/'process.json',{'pid':proc.pid,'started_at':now(),'binary':str(binary_path(root))})
        try:
            while proc.poll() is None and not ending:
                current=request.read_bytes() if request.exists() else None
                if current!=before:break
                time.sleep(.25)
        finally:
            kill_group(proc)
            (sd/'process.json').unlink(missing_ok=True)
        return 0 if ending or current!=before else (proc.returncode or 1)

def admin_headers(root):
    key=os.environ.get('HUB_ADMIN_TOKEN','').strip()
    if not key:
        p=Path(root)/'data/admin-token.txt'
        if not p.is_file() or p.is_symlink():raise Stop('Local admin key unavailable; use Manager connection settings manually')
        key=p.read_text().strip()
    if len(key)<24:raise Stop('Invalid local administrator key')
    return {'Authorization':'Bearer '+key}

def desired_config(old):
    out=dict(old)
    out.update(enabled=True,base_url=f'http://127.0.0.1:{PORT}/v1',model=MODEL,non_thinking=True,
               max_tokens=min(old.get('max_tokens',256),256),timeout_seconds=max(old.get('timeout_seconds',180),300))
    return out

def configure(root,action):
    root=Path(root);sd=state_dir(root)
    with lock(root,'admin-change.lock'):
        hs=admin_headers(root)
        old_status=http_json(8088,'/api/llm/config',headers=hs);old=old_status['config']
        if any(old_status.get('counts',{}).get(k,0) for k in ('queued','running')) or old_status.get('active_id'):
            raise Stop('Finish/cancel pending LLM work before configuration changes')
        backup=sd/'manager-before.json';applied=sd/'manager-applied.json'
        if action=='enable':
            validate_ready(root);speech_guard(root);inspect_backend()
            new=desired_config(old)
            if new==old:print('ALREADY ENABLED: no requests were submitted.');return old_status
            if not backup.exists():atomic_json(backup,{'config':old,'sha256':hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest(),'at':now()})
        elif action=='disable':new=dict(old,enabled=False)
        elif action=='restore':
            saved=read_json(backup);new=saved['config']
            if hashlib.sha256(json.dumps(new,sort_keys=True).encode()).hexdigest()!=saved['sha256']:raise Stop('Backup checksum mismatch')
            last=read_json(applied)['config']
            if {k:v for k,v in old.items() if k!='enabled'}!={k:v for k,v in last.items() if k!='enabled'}:
                raise Stop('Manager settings changed since enable. Restore manually; nothing overwritten.')
        else:raise Stop('Unknown configuration action')
        # Re-check immediately; the existing API has no configuration ETag.
        if http_json(8088,'/api/llm/config',headers=hs)['config']!=old:raise Stop('Concurrent Manager edit; retry after closing its settings dialog')
        result=http_json(8088,'/api/llm/config',new,method='PUT',headers=hs)
        if result['config']!=new:raise Stop('Configuration read-back mismatch')
        if action=='enable':atomic_json(applied,{'config':new,'at':now()})
        print(action.upper()+': model='+new['model']+'; enabled='+str(new['enabled'])+'; no requests were automatically sent.')
        return result

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,default=ROOT)
    ap.add_argument('action',choices=['download','smoke','serve','status','enable','disable','restore','probe'])
    ns=ap.parse_args();root=ns.root.resolve();os.umask(0o077)
    try:
        if ns.action=='download':
            with lock(root,'setup.lock'):print(json.dumps(download_model(root),ensure_ascii=False))
        elif ns.action=='smoke':
            with lock(root,'setup.lock'):run_smoke(root)
        elif ns.action=='serve':return serve(root)
        elif ns.action in ('enable','disable','restore'):configure(root,ns.action)
        elif ns.action=='probe':print(json.dumps(inspect_backend(),ensure_ascii=False,indent=2))
        else:
            r=validate_ready(root)
            print(json.dumps({k:r.get(k) for k in ('ok','kind','model','threads','context','cpu_only','non_thinking','commit','at')},ensure_ascii=False,indent=2))
        return 0
    except (Stop,OSError,ValueError,KeyError) as e:
        print('STOP:',str(e),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
