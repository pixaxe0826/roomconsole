#!/usr/bin/env python3
"""Outer Termux service supervisor: retain control of a private PRoot process group.
First request inner graceful shutdown by a private marker. Signals/forced cleanup
only target the process group created by THIS supervisor, never all Python/PRoot.
"""
import argparse, contextlib, os
from pathlib import Path
import signal, subprocess, sys, time
from runtime import ROOT, Stop, atomic_json, kill_group, lock, state_dir

def supervise(root, prefix):
    root=Path(root);prefix=Path(prefix);sd=state_dir(root)
    with lock(root,'outer-service.lock'):
        ending=False
        def halt(*_):
            nonlocal ending
            ending=True
            atomic_json(sd/'stop.request',{'at':time.time_ns(),'sender':'runit-supervisor'})
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,halt)
        args=[str(prefix/'bin/proot-distro'),'login','--bind',str(root)+':/opt/room-hub','roomhub','--',
              '/usr/bin/python3','/opt/room-hub/deploy/termux/qwen-runtime/runtime.py','serve']
        env=dict(os.environ)
        for key in ('HUB_ADMIN_TOKEN','HUB_INGEST_TOKEN','HUB_LLM_API_KEY'):env.pop(key,None)
        proc=subprocess.Popen(args,stdin=subprocess.DEVNULL,start_new_session=True,env=env)
        try:
            while proc.poll() is None and not ending:time.sleep(.25)
            if ending and proc.poll() is None:
                # Re-issue marker to close a race while inner startup validates hashes.
                deadline=time.monotonic()+12
                while proc.poll() is None and time.monotonic()<deadline:
                    atomic_json(sd/'stop.request',{'at':time.time_ns(),'sender':'runit-supervisor'})
                    time.sleep(.5)
        finally:kill_group(proc,grace=3)
        return 0 if ending else (proc.returncode or 1)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=ROOT);a=p.parse_args()
    os.umask(0o077)
    try:
        prefix=os.environ.get('PREFIX')
        if not prefix:raise Stop('Run outside Debian in Termux')
        raise SystemExit(supervise(a.root.resolve(),prefix))
    except (OSError,Stop) as e:print('STOP:',e,file=sys.stderr);raise SystemExit(1)
