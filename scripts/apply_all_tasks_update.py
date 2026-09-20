"""Verified source-only patch installer. Never imports the app or reads runtime data.

Use the copy at the root of the supplied patch ZIP:
  python apply_update.py --target ~/room-hub --check
  python apply_update.py --target ~/room-hub --apply --server-stopped
  python apply_update.py --target ~/room-hub --rollback BACKUP --server-stopped

A running TCP listener on --port (8088 by default) blocks writes. --server-stopped
also confirms that another worker/port is not using the target. Stop Room Hub only,
not SSH/nginx/cloudflared. No service, Android, environment or private data changes.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,socket,stat,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath

PATCH_ID='room-hub-0.1.2-alltasks1'
ROOT_FILES={'.gitignore','VERSION','README.md','README_KO.md','CHANGELOG.md','AGENTS.md','SECURITY.md','run.py','compose.yaml'}
ROOT_DIRS={'.github','app','web','widgets','docs','scripts','tests','previews','deploy'}
DENY={'data','.git','.venv','.venv-v35','.termux','backups','secrets','node_modules'}

def digest(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def source_digest(data:bytes)->str:
 # Accept Git/Windows line-ending conversion, but not source code changes.
 try:text=data.decode('utf-8-sig')
 except UnicodeDecodeError:return digest(data)
 return digest(text.replace('\r\n','\n').encode('utf-8'))

def safe_path(root:Path,relative:str)->Path:
 p=PurePosixPath(relative)
 if not p.parts or p.is_absolute() or '..' in p.parts or '\\' in relative or any(part in DENY for part in p.parts):
  raise ValueError('Unsafe patch path: '+relative)
 if len(p.parts)==1 and relative not in ROOT_FILES:raise ValueError('Unexpected root file: '+relative)
 if len(p.parts)>1 and p.parts[0] not in ROOT_DIRS:raise ValueError('Unexpected source directory: '+relative)
 if p.name.startswith('.env') or p.name.endswith('-token.txt'):raise ValueError('Private file path forbidden')
 current=root
 for part in p.parts:
  current=current/part
  if current.is_symlink():raise ValueError('Symlink not allowed: '+relative)
 return current

def read_manifest(package:Path)->dict:
 manifest=json.loads((package/'PATCH_MANIFEST.json').read_text('utf-8'))
 if manifest.get('patch_id')!=PATCH_ID:raise ValueError('Incorrect patch manifest')
 entries=manifest.get('files',[]);seen=set()
 if not entries:raise ValueError('Empty patch')
 for entry in entries:
  rel=entry['path']
  if rel in seen:raise ValueError('Duplicate patch path: '+rel)
  seen.add(rel);src=safe_path(package/'files',rel)
  if not src.is_file() or digest(src.read_bytes())!=entry['sha256']:raise ValueError('Payload hash mismatch: '+rel)
 return manifest

def plan_update(package:Path,target:Path)->tuple[dict,list]:
 manifest=read_manifest(package)
 if not (target/'app/main.py').is_file() or not (target/'web/client.html').is_file():
  raise ValueError('Choose the existing project root containing app/, web/ and run.py')
 plan=[];conflicts=[]
 for entry in manifest['files']:
  path=safe_path(target,entry['path']);exists=path.exists()
  if exists and not path.is_file():raise ValueError('Target is not a file: '+entry['path'])
  current=path.read_bytes() if exists else None
  if current is not None and source_digest(current)==entry['source_sha256']:
   continue
  if current is not None and source_digest(current) not in entry.get('before',[]):conflicts.append(entry['path']);continue
  if current is None and entry.get('required_existing'):conflicts.append(entry['path']+' (missing)');continue
  plan.append({**entry,'existed':exists,'old_sha256':digest(current) if current is not None else None,'mode':stat.S_IMODE(path.stat().st_mode) if exists else 0o644})
 if conflicts:raise ValueError('Unknown local changes; nothing was replaced:\n  '+'\n  '.join(conflicts))
 return manifest,plan

def atomic_write(path:Path,data:bytes,mode:int=0o644)->None:
 path.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.NamedTemporaryFile(prefix='.roomhub-update-',dir=path.parent,delete=False) as f:
  temp=Path(f.name)
  try:f.write(data);f.flush();os.fsync(f.fileno())
  except BaseException:temp.unlink(missing_ok=True);raise
 try:os.chmod(temp,mode);os.replace(temp,path)
 finally:temp.unlink(missing_ok=True)

def confirm_stopped(confirmed:bool,port:int)->None:
 if not confirmed:raise ValueError('Stop Room Hub first, then specify --server-stopped')
 if not 1<=port<=65535:raise ValueError('Invalid port')
 with socket.socket() as s:
  s.settimeout(1)
  if s.connect_ex(('127.0.0.1',port))==0:raise ValueError(f'Port {port} is still listening. Stop Room Hub; do not stop SSH or overwrite a running server.')

def apply_update(package:Path,target:Path,backup_parent:Path|None=None)->Path|None:
 manifest,plan=plan_update(package,target)
 if not plan:return None
 parent=backup_parent or target.parent/'room-hub-update-backups'
 parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 if parent.is_symlink():raise ValueError('Backup parent must not be a symlink')
 backup=Path(tempfile.mkdtemp(prefix=PATCH_ID+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-',dir=parent));os.chmod(backup,0o700)
 for e in plan:
  if e['existed']:
   dest=backup/'files'/e['path'];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(safe_path(target,e['path']),dest)
 record={'patch_id':PATCH_ID,'target':str(target.resolve()),'files':plan,'status':'prepared'}
 (backup/'BACKUP_RECORD.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
 applied=[]
 try:
  for e in plan:
   path=safe_path(target,e['path'])
   if e['existed'] and (not path.is_file() or digest(path.read_bytes())!=e['old_sha256']):raise ValueError('File changed during update: '+e['path'])
   if not e['existed'] and path.exists():raise ValueError('File appeared during update: '+e['path'])
   atomic_write(path,safe_path(package/'files',e['path']).read_bytes(),e['mode']);applied.append(e)
 except BaseException:
  for e in reversed(applied):
   path=safe_path(target,e['path'])
   if e['existed']:atomic_write(path,(backup/'files'/e['path']).read_bytes(),e['mode'])
   else:path.unlink(missing_ok=True)
  record['status']='rolled-back-after-error';(backup/'BACKUP_RECORD.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8');raise
 record['status']='applied';(backup/'BACKUP_RECORD.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
 return backup

def rollback(target:Path,backup:Path)->None:
 record=json.loads((backup/'BACKUP_RECORD.json').read_text('utf-8'))
 if record.get('patch_id')!=PATCH_ID or record['target']!=str(target.resolve()):raise ValueError('Backup does not belong to this target')
 for e in record['files']:
  path=safe_path(target,e['path'])
  if not path.is_file() or source_digest(path.read_bytes())!=e['source_sha256']:raise ValueError('Current file changed after update; rollback blocked: '+e['path'])
  if e['existed']:
   original=safe_path(backup/'files',e['path'])
   if not original.is_file() or digest(original.read_bytes())!=e['old_sha256']:raise ValueError('Backup hash mismatch: '+e['path'])
 for e in reversed(record['files']):
  path=safe_path(target,e['path'])
  if e['existed']:atomic_write(path,(backup/'files'/e['path']).read_bytes(),e['mode'])
  else:path.unlink()
 record['status']='rolled-back';(backup/'BACKUP_RECORD.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')

def main()->int:
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--target',type=Path,required=True);parser.add_argument('--package',type=Path,default=Path(__file__).resolve().parent)
 group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--check',action='store_true');group.add_argument('--apply',action='store_true');group.add_argument('--rollback',type=Path)
 parser.add_argument('--server-stopped',action='store_true');parser.add_argument('--port',type=int,default=8088)
 args=parser.parse_args();target=args.target.expanduser().resolve();package=args.package.expanduser().resolve()
 try:
  if args.rollback:
   confirm_stopped(args.server_stopped,args.port);rollback(target,args.rollback.expanduser().resolve());print('ROLLED BACK. Start Room Hub and reload the browser.');return 0
  _,plan=plan_update(package,target)
  if args.check:
   print('READY: '+str(len(plan))+' source files to update. No files changed.');return 0
  if not plan:print('ALREADY APPLIED: '+PATCH_ID);return 0
  confirm_stopped(args.server_stopped,args.port)
  backup=apply_update(package,target)
  print('APPLIED: '+PATCH_ID+'\nBACKUP: '+str(backup)+'\nStart Room Hub, check /healthz for 0.1.2, then reload Manager and iPad.\nPrivate data, sessions, keys, layout, services and wake-lock settings were not changed.')
  return 0
 except (ValueError,OSError,KeyError,json.JSONDecodeError) as e:print('STOP: '+str(e),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
