#!/usr/bin/env python3
"""Offline, source-only 0.1.6 -> 0.1.7 updater. Never edits a DB or runtime configuration."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import sys
import tempfile
import uuid

PATCH_ID='room-hub-0.1.7-notes-alarms1'
ALLOWED={'VERSION','app/main.py','app/life.py','app/capabilities.py','app/command_semantics.py',
 'web/client.html','web/client.js','web/manager.html','web/manager.js','web/manager-llm.js','web/life.js','web/life.css',
 'widgets/note/manifest.json','widgets/note/widget.js','widgets/note/style.css',
 'widgets/alarms/manifest.json','widgets/alarms/widget.js','widgets/alarms/style.css',
 'docs/NOTES_ALARMS.md','docs/UPDATE_017.md','docs/TEST_REPORT_017.md',
 'README.md','CHANGELOG.md','SECURITY.md','AGENTS.md','docs/CURRENT_STATUS.md',
 'docs/API.md','docs/WIDGET_API.md','docs/README.md','run.py','compose.yaml','deploy/truenas.example.yaml'}
PROTECTED=('data/speech-config.json','data/stt-accuracy.json','data/admin-token.txt','data/ingest-token.txt','.env')


def digest(data):return hashlib.sha256(data).hexdigest()
def source_digest(data):return digest(data.replace(b'\r\n',b'\n'))


def safe_path(root,relative):
    p=PurePosixPath(relative)
    if relative not in ALLOWED or p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError('Non-allowlisted update path: '+relative)
    dest=root.joinpath(*p.parts)
    current=root
    if root.is_symlink():raise ValueError('Target root is a symlink')
    for part in p.parts:
        current=current/part
        if current.is_symlink():raise ValueError('Symlink target refused: '+relative)
    if dest.exists() and not dest.is_file():raise ValueError('Non-file target: '+relative)
    return dest


def root_path(value):
    root=Path(value).absolute()
    if '..' in root.parts:raise ValueError('Use an absolute project path without ..')
    for p in [root,*root.parents]:
        if p.is_symlink():raise ValueError('Symlink project/ancestor is refused')
    if not root.is_dir() or not (root/'app/main.py').is_file():raise ValueError('Not an existing Room Hub directory')
    return root


def atomic(path,data,mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.room-hub-update-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def load(package):
    manifest=json.loads((package/'PATCH_MANIFEST.json').read_text('utf-8'))
    if manifest.get('patch_id')!=PATCH_ID or manifest.get('from_version')!='0.1.6' or manifest.get('to_version')!='0.1.7':
        raise ValueError('Wrong update manifest')
    files=manifest.get('files',[])
    if not files or len(files)>len(ALLOWED) or len({f['path'] for f in files})!=len(files):raise ValueError('Invalid/duplicate manifest entries')
    for f in files:
        rel=f['path'];src=safe_path(package/'files',rel)
        data=src.read_bytes()
        if digest(data)!=f['sha256']:raise ValueError('Damaged payload: '+rel)
        if f.get('before') is not None and not re.fullmatch('[0-9a-f]{64}',f['before']):raise ValueError('Invalid baseline digest')
    if not any(f['path']=='VERSION' for f in files):raise ValueError('Version entry missing')
    return manifest


def plan(package,target):
    manifest=load(package);version=(target/'VERSION').read_text('utf-8').strip()
    if version not in {'0.1.6','0.1.7'}:raise ValueError('Only an existing 0.1.6 installation or this applied 0.1.7 is supported')
    changes=[];already=True
    for f in manifest['files']:
        dest=safe_path(target,f['path']);data=dest.read_bytes() if dest.exists() else None
        expected=(package/'files'/f['path']).read_bytes()
        if data==expected:continue
        already=False
        if (data is None)!=(f['before'] is None) or data is not None and source_digest(data)!=f['before']:
            raise ValueError('Unknown local change; nothing written: '+f['path'])
        changes.append(f)
    if not already and version!='0.1.6':raise ValueError('Partially changed 0.1.7; inspect the recorded backup instead of overwriting')
    return manifest,changes


def check_stopped(stopped,port):
    if not stopped:raise ValueError('--server-stopped is required after stopping Room Hub')
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.8):pass
    except ConnectionRefusedError:return
    except OSError as exc:
        # A timeout is not proof that an application is stopped.
        raise ValueError('Cannot verify closed Room Hub port') from exc
    raise ValueError(f'Port {port} is still open; stop Room Hub only')


def private_hashes(target):
    # Hash only bounded config/key files, never model weights, databases or recordings.
    items={}
    candidates=[target/p for p in PROTECTED]
    custom=os.getenv('HUB_DATA_DIR')
    if custom:
        d=Path(custom)
        if not d.is_absolute():raise ValueError('Export an absolute HUB_DATA_DIR for custom data storage')
        candidates += [d/p.split('/',1)[1] for p in PROTECTED if p.startswith('data/')]
    for p in candidates:
        if p.exists():
            if p.is_symlink() or not p.is_file() or p.stat().st_size>1024*1024:raise ValueError('Unusual configuration path; inspect locally')
            items[str(p)]=digest(p.read_bytes())
    return items


def restore(target,backup,receipt):
    for row in reversed(receipt['files']):
        dest=safe_path(target,row['path'])
        if row['existed']:
            src=safe_path(backup/'originals',row['path']);data=src.read_bytes()
            if digest(data)!=row['before_sha256']:raise ValueError('Corrupt backup: '+row['path'])
            atomic(dest,data,row['mode'])
        elif dest.exists():dest.unlink()


def apply(package,target,*,stopped=False,port=8088):
    manifest,changes=plan(package,target)
    if not changes:return None
    check_stopped(stopped,port);protected=private_hashes(target)
    backup_root=target.parent/'room-hub-update-backups'
    if backup_root.is_symlink():raise ValueError('Backup root is a symlink')
    backup=backup_root/(PATCH_ID+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6])
    backup.mkdir(parents=True,mode=0o700);receipt={'patch_id':PATCH_ID,'target':str(target),'state':'installing','files':[]}
    # Back up and verify the entire plan before the first source file changes.
    for f in changes:
        dest=safe_path(target,f['path']);existed=dest.exists();data=dest.read_bytes() if existed else None
        row={'path':f['path'],'existed':existed,'before_sha256':digest(data) if existed else None,
             'after_sha256':f['sha256'],'mode':stat.S_IMODE(dest.stat().st_mode) if existed else 0o644}
        if existed:atomic(safe_path(backup/'originals',f['path']),data,row['mode'])
        receipt['files'].append(row)
    journal=backup/'receipt.json';atomic(journal,json.dumps(receipt,indent=2).encode(),0o600)
    print('BACKUP: '+str(backup),flush=True)
    written=[]
    try:
        for f in sorted(changes,key=lambda x:x['path']=='VERSION'):
            dest=safe_path(target,f['path']);data=(package/'files'/f['path']).read_bytes()
            # Recheck against concurrent manual edits before replacement.
            prior=next(x for x in receipt['files'] if x['path']==f['path'])
            current=dest.read_bytes() if dest.exists() else None
            if (digest(current) if current is not None else None)!=prior['before_sha256']:raise ValueError('Source changed during update')
            atomic(dest,data,prior['mode']);written.append(f['path'])
        if private_hashes(target)!=protected:raise ValueError('Private configuration changed during update; source rollback required')
        receipt['state']='applied';atomic(journal,json.dumps(receipt,indent=2).encode(),0o600)
    except BaseException:
        undo={**receipt,'files':[r for r in receipt['files'] if r['path'] in written]}
        for row in undo['files']:
            dest=safe_path(target,row['path'])
            if not dest.exists() or digest(dest.read_bytes())!=row['after_sha256']:
                raise ValueError('Concurrent source edit; preserve backup and inspect before rollback') from None
        restore(target,backup,undo)
        receipt['state']='rolled_back';atomic(journal,json.dumps(receipt,indent=2).encode(),0o600)
        raise
    return backup


def rollback(target,backup,*,stopped=False,port=8088):
    check_stopped(stopped,port)
    backup=Path(backup).absolute()
    for p in [backup,*backup.parents]:
        if p.is_symlink():raise ValueError('Symlink backup refused')
    if (backup/'receipt.json').is_symlink():raise ValueError('Symlink receipt refused')
    receipt=json.loads((backup/'receipt.json').read_text('utf-8'))
    if receipt.get('patch_id')!=PATCH_ID or receipt.get('target')!=str(target):raise ValueError('Backup belongs to another update/target')
    if receipt.get('state')=='rolled_back':return
    if receipt.get('state') not in {'applied','installing'}:raise ValueError('Unknown backup state')
    if len({r['path'] for r in receipt['files']})!=len(receipt['files']):raise ValueError('Duplicate rollback entries')
    for row in receipt['files']:
        dest=safe_path(target,row['path']);current=digest(dest.read_bytes()) if dest.exists() else None
        allowed={row['after_sha256']}
        if receipt['state']=='installing':allowed.add(row['before_sha256'])
        if current not in allowed:raise ValueError('File edited since update; refusing rollback: '+row['path'])
        if row['existed']:
            src=safe_path(backup/'originals',row['path'])
            if digest(src.read_bytes())!=row['before_sha256']:raise ValueError('Corrupt backup: '+row['path'])
    protected=private_hashes(target);restore(target,backup,receipt)
    if private_hashes(target)!=protected:raise ValueError('Private configuration changed independently')
    receipt['state']='rolled_back';atomic(backup/'receipt.json',json.dumps(receipt,indent=2).encode(),0o600)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target',required=True)
    p.add_argument('--package',default=str(Path(__file__).resolve().parent));p.add_argument('--port',type=int,default=8088)
    p.add_argument('--server-stopped',action='store_true');g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true');g.add_argument('--apply',action='store_true');g.add_argument('--rollback')
    args=p.parse_args()
    try:
        target=root_path(args.target);package=Path(args.package).absolute()
        if not 1<=args.port<=65535:raise ValueError('Invalid port')
        if args.check:
            _,changes=plan(package,target);private_hashes(target)
            print('READY: '+str(len(changes))+' source files; DB/models/keys/settings/services untouched' if changes else 'ALREADY APPLIED')
        elif args.apply:
            result=apply(package,target,stopped=args.server_stopped,port=args.port)
            print('APPLIED: '+PATCH_ID if result else 'ALREADY APPLIED')
        else:
            rollback(target,args.rollback,stopped=args.server_stopped,port=args.port)
            print('ROLLED BACK: source only; new DB tables and saved notes/alarms retained')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print('STOP: '+str(exc),file=sys.stderr);return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
