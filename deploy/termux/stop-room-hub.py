"""Stop only Room Hub, including the exact Python child behind the PRoot wrapper.

No blanket pkill/python/proot kills. Source paths are checked again before each
signal. Unknown processes/listeners are left untouched. Default: graceful TERM.
--force permits SIGKILL to a still-matching Room Hub Python child after timeout.
Use in the OUTER Termux shell. Existing SSH/nginx/cloudflared are never selected.
"""
from __future__ import annotations
import argparse,os,re,signal,socket,subprocess,time
from pathlib import Path


def listening(port):
    with socket.socket() as s:
        s.settimeout(.5);return s.connect_ex(('127.0.0.1',port))==0


def read_args(pid):
    try:return [s.decode(errors='replace') for s in (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0') if s]
    except OSError:return []


def matching(args,root):
    return (len(args)>=2 and re.fullmatch(r'python(?:3(?:\.\d+)?)?',Path(args[0]).name) is not None
            and args[1] in {str(root/'deploy/termux/server.py'),'/opt/room-hub/deploy/termux/server.py'})


def candidates(root):
    found={}
    for p in Path('/proc').glob('[0-9]*'):
        args=read_args(int(p.name))
        if matching(args,root):found[int(p.name)]=args
    return found


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path.home()/'room-hub');parser.add_argument('--force',action='store_true');parser.add_argument('--timeout',type=int,default=20)
    args=parser.parse_args();root=args.root.resolve();prefix=os.getenv('PREFIX')
    if not prefix:raise SystemExit('Run in outer Termux (PREFIX is missing).')
    service=Path(prefix)/'var/service/room-hub'
    if not (service/'run').exists():raise SystemExit('Room Hub service not found; no process was signalled.')
    # Request down first so runit does not respawn the child we are stopping.
    subprocess.run(['sv','-w','2','down',str(service)],check=False)
    found=candidates(root)
    if len(found)>1:raise SystemExit('Multiple matching Python servers; stop and inspect before terminating anything.')
    for pid,original in found.items():
        if read_args(pid)==original:
            print('Sending TERM to exact Room Hub Python PID',pid)
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
    deadline=time.monotonic()+args.timeout
    while listening(8088) and time.monotonic()<deadline:time.sleep(.4)
    if listening(8088) and args.force:
        for pid,original in found.items():
            if read_args(pid)==original:
                print('Sending KILL to still-matching Room Hub Python PID',pid)
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
        time.sleep(2)
    if listening(8088):
        raise SystemExit('STOP: 8088 is still reachable. Do NOT apply an update. Try --force only after checking the printed PID; unknown listeners were not killed.')
    # The listener is gone; now clear a stranded wrapper via this exact runit service.
    subprocess.run(['sv','-w','3','force-stop',str(service)],check=False)
    print('STOPPED: port 8088 is closed. Recheck before applying. Other services unchanged.')
    return 0
if __name__=='__main__':raise SystemExit(main())
