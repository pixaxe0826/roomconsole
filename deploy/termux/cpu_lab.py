#!/usr/bin/env python3
"""Base-only 1..8-thread control, read-only recording, optional exact-audio replay.
Run in OUTER Termux with its Python. No new packages, root, GPU enablement or model change.
"""
from __future__ import annotations
import argparse,json,os,statistics,sys
from pathlib import Path
from cpu_lab_common import Stop,atomic,encode,set_threads,snapshot
from cpu_lab_monitor import record

def compare(before,after,output):
    b=json.loads(Path(before).read_text());a=json.loads(Path(after).read_text())
    reason=[]
    for name,s in [('before',b),('after',a)]:
        if s.get('schema')!=2:reason.append(name+': old logger has no server job timing/input identity; reference only')
        if not s.get('validation',{}).get('expected_base_and_threads_observed'):reason.append(name+': base/threads/process not fully confirmed')
    if not reason:
        for k in ('model_sha256','engine_sha256','speech_source_sha256','settings_except_threads_sha256'):
            if not b['configuration'].get(k) or b['configuration'].get(k)!=a['configuration'].get(k):reason.append('Mismatching condition: '+k)
    def timed(s):
        out={}
        for j in s.get('jobs',[]):
            if j.get('status')=='succeeded' and j.get('elapsed',0) and j.get('audio_sha256'):
                out.setdefault(j['audio_sha256'],[]).append(j)
        return out
    left,right=timed(b),timed(a);matches=[]
    for sha in sorted(left.keys()&right.keys()):
        l,r=left[sha],right[sha]
        durations=[j.get('duration') for j in l+r]
        if any(d is None for d in durations) or max(durations)-min(durations)>.001:
            reason.append('Audio duration mismatch despite same hash');continue
        x=statistics.median(j['elapsed'] for j in l);y=statistics.median(j['elapsed'] for j in r)
        matches.append({'audio_sha256':sha,'before_trials':len(l),'after_trials':len(r),'before_median_seconds':x,'after_median_seconds':y,
                        'speedup_before_over_after':x/y,'time_reduction_percent':100*(1-y/x),'scope':'same-file server processing; not upload/UI latency'})
    if not matches:reason.append('No identical saved-audio SHA256 + succeeded server timing in both logs; cannot claim controlled speedup.')
    result={'schema':1,'before':Path(before).name,'after':Path(after).name,
            'before_threads':b.get('configuration',{}).get('threads'),'after_threads':a.get('configuration',{}).get('threads'),
            'controlled_pair_available':not reason,'warnings':reason,'matched_audio_results':matches if not reason else [],
            'notes':['Only matching audio/model/engine/settings are numerically compared.','Trial order, starting temperature and cache warmth still affect results. Repeat 4/8 alternately with cooling.','CPU percentages and partial observed process seconds alone do not establish speedup.']}
    if output:atomic(Path(output),encode(result))
    print(json.dumps(result,ensure_ascii=False,indent=2));return result

def main(argv=None):
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path.home()/'room-hub')
    sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('status');t=sub.add_parser('threads');t.add_argument('count',type=int)
    r=sub.add_parser('record');r.add_argument('--label',default='base-t8');r.add_argument('--expect-threads',type=int,default=8)
    r.add_argument('--seconds',type=int,default=180);r.add_argument('--interval',type=float,default=1)
    r.add_argument('--replay-from',type=Path,help='EXPLICIT one-time duplicate upload of the one succeeded job in this summary; adds inbox record.')
    r.add_argument('--stop-after-job',action='store_true');r.add_argument('--output-dir',type=Path)
    c=sub.add_parser('compare');c.add_argument('--before',type=Path,required=True);c.add_argument('--after',type=Path,required=True);c.add_argument('--output',type=Path)
    a=p.parse_args(argv)
    try:
        if a.cmd=='status':print(json.dumps(snapshot(a.root,True),ensure_ascii=False,indent=2))
        elif a.cmd=='threads':set_threads(a.root,a.count)
        elif a.cmd=='record':record(a.root,a.label,a.seconds,a.interval,a.expect_threads,a.replay_from,a.stop_after_job,a.output_dir)
        elif a.cmd=='compare':compare(a.before,a.after,a.output)
        return 0
    except (Stop,OSError,ValueError) as ex:print('STOP:',ex,file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
