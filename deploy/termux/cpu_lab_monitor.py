"""Read-only V35 CPU/GPU/RAM/job sampler. Missing permissions remain null, never 0.
No tokens/audio/transcripts/full argv in output. GPU is device-wide KGSL window
ratio, not per-process usage; reading gpubusy may clear its saved off-state window.
"""
from __future__ import annotations
import csv, datetime as dt, hashlib, html, json, math, os, re, signal, sqlite3, statistics
import time, urllib.error, urllib.request, uuid, zipfile
from pathlib import Path
from cpu_lab_common import Stop, atomic, config, db_read, digest, encode, ensure_idle, lock, no_links, snapshot, stamp

PROC=Path('/proc');SYS=Path('/sys')
def read(path):
    try:return Path(path).read_text(errors='replace').strip(), 'ok'
    except PermissionError:return None,'permission_denied'
    except FileNotFoundError:return None,'not_present'
    except OSError:return None,'read_error'
def num(text,divisor=1):
    try:
        v=float(text.split()[0])/divisor
        return v if math.isfinite(v) else None
    except (AttributeError,ValueError,IndexError,TypeError):return None
def status(text):return dict(x.split(':',1) for x in (text or '').splitlines() if ':' in x)
def procstat(text):
    f=text[text.rfind(')')+2:].split()
    if len(f)<37:raise ValueError('short proc stat')
    return {'state':f[0],'ticks':int(f[11])+int(f[12]),'start_ticks':int(f[19]),
            'os_threads':int(f[17]),'last_cpu':int(f[36]),'nice':int(f[16])}
def option(argv,names):
    return next((argv[i+1] for i,a in enumerate(argv[:-1]) if a in names),None)
def discover(proc=PROC):
    found=[]
    for p in proc.glob('[0-9]*'):
        try:
            if p.stat().st_uid!=os.getuid():continue
            a=[b.decode('utf8','replace') for b in (p/'cmdline').read_bytes().split(b'\0') if b]
            if not a or Path(a[0]).name!='whisper-cli':continue
            n=option(a,{'-t','--threads'});model=option(a,{'-m','--model'})
            found.append((int(p.name),{'model':Path(model).name if model else None,
                'configured_threads':int(n) if n and n.isdecimal() else None,
                'no_gpu_flag':bool({'-ng','--no-gpu'}&set(a))}))
        except (OSError,ValueError):continue
    return found

def process(pid,proc=PROC):
    p=proc/str(pid)
    try:
        row=procstat((p/'stat').read_text())
        if row['state'] in {'Z','X'}:return None
        s=status(read(p/'status')[0]);roll=status(read(p/'smaps_rollup')[0])
        row.update(rss_mib=num(s.get('VmRSS'),1024),hwm_mib=num(s.get('VmHWM'),1024),
            pss_mib=num(roll.get('Pss'),1024),swap_mib=num(s.get('VmSwap'),1024),allowed_cpus=(s.get('Cpus_allowed_list') or '').strip() or None)
        return row
    except (OSError,ValueError):return None

def cpu_pct(old,new,elapsed,hz):
    if not old or elapsed<=0 or old['start_ticks']!=new['start_ticks'] or new['ticks']<old['ticks']:return None
    return 100*(new['ticks']-old['ticks'])/hz/elapsed

class GPU:
    def __init__(self,sysroot=SYS):
        self.root=Path(sysroot)/'class/kgsl/kgsl-3d0'
        self.permissions={};self.previous=None
    def sample(self):
        raw,st=read(self.root/'gpubusy');self.permissions['gpubusy']=st
        busy=total=pct=None;why=st
        if raw is not None:
            try:
                tokens=raw.split()
                if len(tokens)!=2:raise ValueError()
                busy,total=map(int,tokens)
                if busy<0 or total<0 or busy>total:raise ValueError()
                if total>0:pct=100*busy/total;why='driver_window'
                else:why='no_valid_window'  # 0/0 does NOT establish idle/0%.
            except ValueError:why='unrecognized_format';busy=total=None
        freq=None;source=None
        for name in ('gpuclk','devfreq/cur_freq'):
            text,st=read(self.root/name);self.permissions[name]=st
            v=num(text,1_000_000)
            if v is not None and 0<=v<10000:freq=v;source=name;break
        return {'gpu_percent_driver_window':pct,'gpu_busy_raw':busy,'gpu_total_raw':total,
                'gpu_status':why,'gpu_freq_mhz':freq,'gpu_freq_source':source,
                'gpu_scope':'device-wide; driver-defined recent window; NOT Whisper-only'}

class Hardware:
    def __init__(self,proc=PROC,sysroot=SYS):
        self.proc=Path(proc);self.sysroot=Path(sysroot);self.gpu=GPU(sysroot);self.old_cpu={};self.permissions={}
        self.policies=list((self.sysroot/'devices/system/cpu/cpufreq').glob('policy*'))
        self.zones=[]
        for p in (self.sysroot/'class/thermal').glob('thermal_zone*'):
            name,st=read(p/'type')
            # Strict V35 sensor-name allowlist; no ibat/vbat/soc/step as temperatures.
            if name and (re.fullmatch(r'cpu\d+-(?:gold|silver)-usr',name) or re.fullmatch(r'gpu\d+-usr',name) or name=='battery'):
                self.zones.append((p,name))
    def sample(self):
        text,ms=read(self.proc/'meminfo');m=status(text);freq={};groups={};temps={};cores={}
        for p in self.policies:
            groups[p.name]=read(p/'related_cpus')[0]
            for f in ('scaling_cur_freq','scaling_max_freq','cpuinfo_max_freq'):
                v,st=read(p/f);freq[p.name+':'+f+'_mhz']=num(v,1000)
        for p,name in self.zones:
            v,st=read(p/'temp');v=num(v,1000)
            temps[p.name+':'+name]=v if v is not None and -40<=v<=180 else None
        raw,st=read(self.proc/'stat');self.permissions['proc_stat']=st;self.permissions['meminfo']=ms
        if raw:
            for line in raw.splitlines():
                f=line.split()
                if not re.fullmatch(r'cpu\d+',f[0] if f else ''):continue
                try:
                    v=list(map(int,f[1:9]));total=sum(v);idle=v[3]+(v[4] if len(v)>4 else 0);prev=self.old_cpu.get(f[0]);pct=None
                    if prev and total>prev[0] and idle>=prev[1]:pct=100*(1-(idle-prev[1])/(total-prev[0]))
                    cores[f[0]]=pct if pct is not None and 0<=pct<=100 else None;self.old_cpu[f[0]]=(total,idle)
                except (ValueError,IndexError):pass
        valid=lambda kind:[v for k,v in temps.items() if kind(k) and v is not None]
        cpu=valid(lambda k:bool(re.search(r':cpu\d+-(gold|silver)-usr$',k)))
        batt=valid(lambda k:k.endswith(':battery'))
        return {'host_mem_available_mib':num(m.get('MemAvailable'),1024),'host_mem_total_mib':num(m.get('MemTotal'),1024),
                'host_swap_used_mib':((num(m.get('SwapTotal'))-num(m.get('SwapFree')))/1024 if num(m.get('SwapTotal')) is not None and num(m.get('SwapFree')) is not None else None),
                'cpu_sensor_max_c':max(cpu) if cpu else None,'battery_sensor_c':max(batt) if batt else None,
                'frequency_mhz_json':json.dumps(freq),'cpu_groups_json':json.dumps(groups),'cpu_core_percent_json':json.dumps(cores),
                'temperatures_c_json':json.dumps(temps),'proc_stat_status':st,**self.gpu.sample()}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

def opener():return urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def health(http):
    t=time.monotonic()
    try:
        with http.open('http://127.0.0.1:8088/healthz',timeout=.5) as r:
            good=r.status==200 and json.loads(r.read(4096)).get('status')=='ok'
        return round((time.monotonic()-t)*1000,3),'ok' if good else 'invalid'
    except (OSError,ValueError,urllib.error.URLError):return None,'unavailable'

def jobs_now(root):
    # IDs are local correlation identifiers, not credentials. Do not select text/error/owner/keys.
    with db_read(root) as db:
        return {r['id']:dict(r) for r in db.execute('''SELECT j.id,j.status,j.attempts,j.duration,j.elapsed,j.updated_at,
            v.filename FROM speech_jobs j JOIN voice v ON v.id=j.voice_id ORDER BY j.updated_at DESC LIMIT 128''')}

def safe_source(root,filename):
    if not filename or Path(filename).name!=filename:raise Stop('Invalid stored audio path.')
    p=Path(root)/'data/audio'/filename;no_links(p)
    if not p.is_file() or p.stat().st_size>10*1024*1024:raise Stop('Audio missing or exceeds limit.')
    return p

def public_job(root,row):
    out={k:row[k] for k in ('id','status','attempts','duration','elapsed','updated_at')}
    out['elapsed_scope']='server processing: initial status broadcast + decode/convert + inspect + model load/inference/result read; excludes upload, queue wait, browser display'
    out['rtf']=out['elapsed']/out['duration'] if out.get('elapsed') is not None and out.get('duration') and out['duration']>0 else None
    try:out['audio_sha256']=digest(safe_source(root,row['filename']))
    except (OSError,Stop):out['audio_sha256']=None
    return out

def replay(root,summary_file,http):
    """Explicit optional action: submit ONE copy of the SAME audio via existing local API.
    No transcript auto-action, new inbox row only; no automatic network retries.
    """
    old=json.loads(Path(summary_file).read_text());finished=[j for j in old.get('jobs',[]) if j['status']=='succeeded']
    if len(finished)!=1:raise Stop('Replay input must contain exactly one succeeded job. Use a one-recording capture.')
    job=finished[0]
    with db_read(root) as db:
        row=db.execute('SELECT v.filename,v.media_type FROM speech_jobs j JOIN voice v ON v.id=j.voice_id WHERE j.id=?',(job['id'],)).fetchone()
    if not row:raise Stop('Original audio no longer exists in inbox.')
    p=safe_source(root,row['filename']);blob=p.read_bytes()
    if hashlib.sha256(blob).hexdigest()!=job.get('audio_sha256'):raise Stop('Stored audio changed; refusing unequal comparison.')
    mime=row['media_type'].split(';')[0]
    if mime not in {'audio/mp4','audio/x-m4a','audio/webm','audio/ogg','audio/wav','audio/x-wav','audio/wave','audio/mpeg','audio/flac','audio/aac'}:raise Stop('Unsupported replay MIME.')
    token=os.getenv('HUB_ADMIN_TOKEN')
    if not token:
        tok=Path(root)/'data/admin-token.txt';no_links(tok);token=tok.read_text().strip()
    if not token or any(c in token for c in '\r\n'):raise Stop('Invalid local admin key; key not printed.')
    boundary='roomcpu'+uuid.uuid4().hex;rid='cpu-lab.'+uuid.uuid4().hex
    data=(f'--{boundary}\r\nContent-Disposition: form-data; name="request_id"\r\n\r\n{rid}\r\n'
          f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="cpu-comparison.bin"\r\nContent-Type: {mime}\r\n\r\n').encode()+blob+f'\r\n--{boundary}--\r\n'.encode()
    req=urllib.request.Request('http://127.0.0.1:8088/api/speech/jobs',data=data,
        headers={'Authorization':'Bearer '+token,'Content-Type':'multipart/form-data; boundary='+boundary,'X-Room-Request':'1'},method='POST')
    try:
        with http.open(req,timeout=15) as r:return json.loads(r.read(32768))['id']
    except Exception as e:
        # Do not print request bodies/headers, tokens, transcripts or arbitrary response bodies.
        raise Stop('Replay submit failed; inspect manager inbox before retrying (request may have arrived).') from e

def summary_process(rows,hz):
    first,last=rows[0],rows[-1];elapsed=last['mono']-first['mono'];cpu=(last['ticks']-first['ticks'])/hz
    peak=lambda k:max((r[k] for r in rows if r.get(k) is not None),default=None)
    return {'pid':first['pid'],'pid_start':f"{first['pid']}:{first['start_ticks']}",'model':first['model'],
        'configured_threads':first['configured_threads'],'no_gpu_flag':first['no_gpu_flag'],
        'sample_count':len(rows),'observed_seconds':round(elapsed,4),'sampled_cpu_seconds':round(cpu,4),
        'mean_cpu_percent_one_core_100':100*cpu/elapsed if elapsed>0 else None,
        'peak_cpu_percent_one_core_100':peak('cpu_percent'),'peak_rss_mib':peak('rss_mib'),
        'peak_hwm_mib':peak('hwm_mib'),'peak_pss_mib':peak('pss_mib'),'peak_swap_mib':peak('swap_mib'),
        'allowed_cpus':last['allowed_cpus'],'nice_values':sorted({r['nice'] for r in rows})}

def report(s):
    e=html.escape;fmt=lambda v:'N/A' if v is None else f'{v:.3f}' if isinstance(v,float) else str(v)
    prows=''.join('<tr>'+''.join('<td>'+e(fmt(p.get(k)))+'</td>' for k in ('pid','configured_threads','mean_cpu_percent_one_core_100','peak_rss_mib','peak_swap_mib','observed_seconds'))+'</tr>' for p in s['processes'])
    jrows=''.join('<tr>'+''.join('<td>'+e(fmt(j.get(k)))+'</td>' for k in ('status','duration','elapsed','rtf'))+'</tr>' for j in s['jobs'])
    return f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Room Hub CPU 실측</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:35px auto;padding:24px;line-height:1.7;color:#23332e;background:#f5f7f6}}table{{border-collapse:collapse;background:white;width:100%;margin:15px 0}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px}}h1{{font-size:28px}}</style>
<h1>Room Hub · {e(s['label'])}</h1><p>다국어 base · 설정 {s['configuration']['threads']}스레드 · 관측 {s['duration_s']:.2f}초</p>
<p>GPU는 기기 전체 드라이버 구간 비율이며 Whisper 사용률이 아닙니다. N/A는 확인 불가입니다. CPU 100% = 논리 CPU 하나의 실행 시간입니다.</p>
<h2>Whisper 자원</h2><table><tr><th>PID</th><th>스레드</th><th>평균 CPU%</th><th>최고 RSS MiB</th><th>Swap MiB</th><th>관측 구간 초</th></tr>{prows}</table>
<h2>서버 기록의 실제 처리 시간</h2><p>elapsed는 서버의 기존 monotonic 타이머입니다. 오디오 변환·검사·모델 로딩·추론·결과 읽기를 포함하며 업로드·큐 대기·화면 표시까지의 종단 지연은 아닙니다. 위의 프로세스 관측 시간과 다릅니다.</p>
<table><tr><th>상태</th><th>녹음 초</th><th>처리 초</th><th>RTF</th></tr>{jrows}</table><h2>계측·상태 요약</h2><pre>{e(json.dumps({k:s[k] for k in ('hardware','validation','notes')},ensure_ascii=False,indent=2))}</pre>'''

def record(root,label,seconds=180,interval=1,expect=8,replay_from=None,stop_after_job=False,output_root=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',label):raise Stop('Use a simple ASCII label.')
    if not .5<=interval<=5 or not 10<=seconds<=900:raise Stop('interval=0.5..5, seconds=10..900.')
    root=Path(root).resolve();base=Path(output_root or Path.home()/'room-hub-cpu-tests');no_links(base)
    with lock(root/'data/speech-cpu-lab/.capture.lock'),lock(root/'data/speech-model-tests/.experiment.lock'):
        cfg=snapshot(root,True)
        if cfg['threads']!=expect:raise Stop(f"Expected {expect} threads but config is {cfg['threads']}; no recording started.")
        ensure_idle(root)
        http=opener();ms,ok=health(http)
        if ok!='ok':raise Stop('Local server /healthz failed. Start Room Hub first.')
        initial=jobs_now(root);initial_sig={k:(v['status'],v['attempts'],v['updated_at']) for k,v in initial.items()}
        folder=base/(label+'-'+stamp());folder.mkdir(parents=True,mode=0o700);os.chmod(folder,0o700)
        hz=os.sysconf('SC_CLK_TCK');hw=Hardware();stopped=False;oldhandlers={};observed={};previous={};threadprev={};sysrows=[];jobs={};errors=set();config_changed=False;replay_id=None;replay_done=False;terminal_at=None
        def halt(signum,frame):
            nonlocal stopped
            stopped=True
        for sig in (signal.SIGINT,signal.SIGTERM):oldhandlers[sig]=signal.signal(sig,halt)
        fields=['utc','elapsed_s','host_mem_available_mib','host_mem_total_mib','host_swap_used_mib','cpu_sensor_max_c','battery_sensor_c','gpu_percent_driver_window','gpu_busy_raw','gpu_total_raw','gpu_status','gpu_freq_mhz','gpu_freq_source','gpu_scope','local_health_ms','local_health_status','sampler_work_ms','frequency_mhz_json','cpu_groups_json','cpu_core_percent_json','temperatures_c_json','proc_stat_status']
        pfields=['elapsed_s','pid','start_ticks','model','configured_threads','no_gpu_flag','os_threads','cpu_percent','rss_mib','hwm_mib','pss_mib','swap_mib','allowed_cpus','last_cpu','nice']
        tfields=['elapsed_s','pid','tid','cpu_percent','last_cpu','state','nice']
        fhs=[(folder/n).open('x',encoding='utf-8',newline='') for n in ('system.csv','processes.csv','threads.csv')]
        writers=[csv.DictWriter(f,fs,extrasaction='ignore') for f,fs in zip(fhs,(fields,pfields,tfields))]
        for w in writers:w.writeheader()
        print('MODEL VERIFIED: multilingual base | threads='+str(expect)+' | CPU-only. Logger does not change hardware.',flush=True)
        print('LOG DIR:',folder,flush=True)
        print('READY: wait ~5 seconds, then record/transcribe ONCE on iPad. After result, wait ~5 seconds and Ctrl+C.',flush=True)
        if replay_from:print('EXPLICIT REPLAY: same stored audio will be uploaded once after 5 seconds; adds a new manager inbox item.',flush=True)
        start=time.monotonic();cpu_start=time.process_time()
        try:
            while not stopped and time.monotonic()-start<seconds:
                t=time.monotonic();elapsed=t-start;row=hw.sample();row.update(utc=dt.datetime.now(dt.timezone.utc).isoformat(),elapsed_s=round(elapsed,4))
                row['local_health_ms'],row['local_health_status']=health(http)
                for pid,meta in discover():
                    p=process(pid)
                    if not p:continue
                    mono=time.monotonic();key=f"{pid}:{p['start_ticks']}";old=previous.get(key)
                    p.update(meta,pid=pid,mono=mono,elapsed_s=round(mono-start,4),cpu_percent=cpu_pct(old,p,mono-old['mono'] if old else 0,hz))
                    previous[key]=p;observed.setdefault(key,[]).append(p);writers[1].writerow(p)
                    for tf in (PROC/str(pid)/'task').glob('[0-9]*'):
                        try:
                            q=procstat((tf/'stat').read_text());tm=time.monotonic();k=(pid,int(tf.name),q['start_ticks']);prev=threadprev.get(k)
                            pct=cpu_pct(prev,q,tm-prev['mono'] if prev else 0,hz);q['mono']=tm;threadprev[k]=q
                            writers[2].writerow(dict(elapsed_s=round(tm-start,4),pid=pid,tid=int(tf.name),cpu_percent=pct,last_cpu=q['last_cpu'],state=q['state'],nice=q['nice']))
                        except (OSError,ValueError):pass
                try:
                    for jid,j in jobs_now(root).items():
                        if initial_sig.get(jid)!=(j['status'],j['attempts'],j['updated_at']):jobs[jid]=j
                except (OSError,sqlite3.Error,Stop):errors.add('job_database_read_failed')
                try:config_changed|=digest(root/'data/speech-config.json')!=cfg['config_sha256']
                except OSError:config_changed=True
                row['sampler_work_ms']=round((time.monotonic()-t)*1000,3);sysrows.append(row);writers[0].writerow(row)
                for f in fhs:f.flush()
                active=[p for p in previous.values() if p['mono']>=t]
                cpu_values=[p['cpu_percent'] for p in active if p['cpu_percent'] is not None]
                cpu=sum(cpu_values) if cpu_values else None
                pretty=lambda v:'N/A' if v is None else f'{v:.1f}'
                print(f"{elapsed:6.1f}s | Whisper CPU {pretty(cpu):>6}% | GPU {pretty(row['gpu_percent_driver_window']):>5}% ({row['gpu_status']}) | RAM avail {pretty(row['host_mem_available_mib'])} MiB | CPU sensor {pretty(row['cpu_sensor_max_c'])} C | jobs {len(jobs)}",flush=True)
                if replay_from and not replay_done and elapsed>=5:
                    replay_done=True;replay_id=replay(root,replay_from,http)
                completed=[j for j in jobs.values() if j['status'] in {'succeeded','failed','cancelled'}]
                if stop_after_job and completed and (not replay_from or any(j['id']==replay_id for j in completed)):
                    terminal_at=terminal_at or time.monotonic()
                    if time.monotonic()-terminal_at>=5:break
                time.sleep(max(0,min(interval-(time.monotonic()-t),.2 if stopped else interval)))
        except Stop as ex:errors.add(str(ex));print('NOTICE:',ex,flush=True)
        finally:
            duration=time.monotonic()-start;sampler_cpu=time.process_time()-cpu_start
            for f in fhs:f.close()
            for sig,h in oldhandlers.items():signal.signal(sig,h)
        # Last read after stopping, no transaction held and no backend cancellation.
        try:
            for jid,j in jobs_now(root).items():
                if initial_sig.get(jid)!=(j['status'],j['attempts'],j['updated_at']):jobs[jid]=j
        except (OSError,sqlite3.Error,Stop):errors.add('final_job_database_read_failed')
        def peak(k):return max((r[k] for r in sysrows if r.get(k) is not None),default=None)
        def low(k):return min((r[k] for r in sysrows if r.get(k) is not None),default=None)
        procs=[summary_process(v,hz) for v in observed.values()]
        valid=bool(procs) and not config_changed and all(p['configured_threads']==expect and p['model']=='ggml-base.bin' and p['no_gpu_flag'] for p in procs)
        s={'schema':2,'sampler':'room-hub-cpu-lab-2','label':label,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
           'duration_s':duration,'interval_s':interval,'configuration':cfg,'processes':procs,
           'jobs':[public_job(root,j) for j in jobs.values()],
           'hardware':{'minimum_available_ram_mib':low('host_mem_available_mib'),'peak_cpu_sensor_c':peak('cpu_sensor_max_c'),
                       'peak_battery_sensor_c':peak('battery_sensor_c'),'peak_gpu_driver_window_percent':peak('gpu_percent_driver_window'),
                       'gpu_permissions':hw.gpu.permissions,'system_permissions':hw.permissions,
                       'http_failures':sum(r['local_health_status']!='ok' for r in sysrows),'peak_local_http_ms':peak('local_health_ms'),
                       'sampler_cpu_seconds':sampler_cpu,'max_sampler_work_ms':peak('sampler_work_ms')},
           'validation':{'expected_base_and_threads_observed':valid,'config_changed':config_changed,'errors':sorted(errors)},
           'notes':['Not a complete device benchmark. Sensors and /proc may be inaccessible.',
                    'CPU 100% is one CPU time, not a weighted share of Kryo performance.',
                    'PSR is last execution CPU, not residency. Thread memory is not added.',
                    'GPU ratio is DEVICE-WIDE driver window, can repeat; not each sampling interval or Whisper attribution. 0/0 is N/A.',
                    'CPU governor, clocks, affinity, GPU backend, thermal controls were NOT changed.',
                    'RSS/PSS approximate. CPU/GPU thermal names allowlisted; voltage/current/step nodes excluded.',
                    'Observed_seconds is incomplete process window, NOT total transcription time.',
                    'jobs.elapsed is actual existing server timer; excludes upload/queue/UI latency.',
                    'No audio, transcript, credentials or full command line included. Job IDs and audio hash are correlation metadata.',
                    'Ctrl+C stops only the logger. Submitted jobs continue normally. No automatic restart or retry.']}
        atomic(folder/'summary.json',encode(s));atomic(folder/'report.html',report(s).encode())
        archive=folder.with_suffix('.zip')
        with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
            for name in ('system.csv','processes.csv','threads.csv','summary.json','report.html'):z.write(folder/name,arcname=folder.name+'/'+name)
        atomic(base/('latest-'+label+'.zip'),archive.read_bytes());atomic(base/('latest-'+label+'.summary.json'),encode(s))
        print('SAVED:',archive,flush=True);print('LATEST:',base/('latest-'+label+'.zip'),flush=True)
        print('BASE / THREADS / -ng OBSERVATION:', 'PASS' if valid else 'NOT CONFIRMED (see summary)',flush=True)
        for j in s['jobs']:
            if j['status']=='succeeded':print('SERVER PROCESSING:',round(j['elapsed'],3),'s | audio:',j['duration'],'s | RTF:',round(j['rtf'],3),flush=True)
        return s
