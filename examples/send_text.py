"""Usage: HUB_INGEST_TOKEN=<key> python examples/send_text.py --server http://LAN-IP:8088 --text '내일 할 일에 산책 추가'"""
import argparse,os,uuid
import httpx
p=argparse.ArgumentParser();p.add_argument('--server',required=True);p.add_argument('--text',required=True);p.add_argument('--source',default='custom-adapter');p.add_argument('--request-id');a=p.parse_args()
key=os.getenv('HUB_INGEST_TOKEN')
if not key:raise SystemExit('Set HUB_INGEST_TOKEN. Never put a real key in source control.')
payload={'schema_version':'1','request_id':a.request_id or str(uuid.uuid4()),'source':a.source,'text':a.text,'locale':'ko-KR','metadata':{}}
r=httpx.post(a.server.rstrip('/')+'/api/voice/text',json=payload,headers={'Authorization':'Bearer '+key},timeout=20)
r.raise_for_status();print(r.json())
