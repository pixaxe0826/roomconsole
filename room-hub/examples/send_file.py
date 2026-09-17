"""Upload an audio file or UTF-8 text file. No transcription occurs automatically."""
import argparse,mimetypes,os,uuid
from pathlib import Path
import httpx
p=argparse.ArgumentParser();p.add_argument('--server',required=True);p.add_argument('--file',type=Path,required=True);p.add_argument('--mime');p.add_argument('--source',default='custom-adapter');p.add_argument('--request-id');a=p.parse_args()
key=os.getenv('HUB_INGEST_TOKEN')
if not key:raise SystemExit('Set HUB_INGEST_TOKEN first.')
if not a.file.is_file() or not 0<a.file.stat().st_size<=10*1024*1024:raise SystemExit('File must exist and be no larger than 10MB.')
mime=a.mime or {'.m4a':'audio/mp4','.mp4':'audio/mp4','.wav':'audio/wav','.txt':'text/plain'}.get(a.file.suffix.lower()) or mimetypes.guess_type(a.file.name)[0] or 'application/octet-stream'
with a.file.open('rb') as f:
 r=httpx.post(a.server.rstrip('/')+'/api/voice/upload',headers={'Authorization':'Bearer '+key},data={'request_id':a.request_id or str(uuid.uuid4()),'source':a.source,'locale':'ko-KR'},files={'file':(a.file.name,f,mime)},timeout=60)
r.raise_for_status();print(r.json())
