"""Measure synthetic API reads without STT/LLM, never against the production DB."""
import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('HUB_DATA_DIR',str(ROOT/'artifacts/runtime-data'))
from fastapi.testclient import TestClient
from app.main import create_app
from app.llm import sha


class NoModel:
    calls=0
    async def generate(self,*args):
        self.calls+=1
        raise AssertionError('Unexpected LLM call')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations',type=int,default=30)
    args=parser.parse_args()
    if not 1<=args.iterations<=100:parser.error('iterations must be 1..100')
    model=NoModel();samples={};processing={}
    with tempfile.TemporaryDirectory() as td:
        app=create_app(Path(td)/'data',weather_enabled=False,llm_backend=model)
        with TestClient(app) as api:
            api.headers.update({'Authorization':'Bearer '+app.state.admin_token,'X-Room-Request':'1'})
            state=api.get('/api/state').json();layout=state['layout'];layout['rows']=8
            layout['widgets'].append({'id':'note-test','type':'note','title':'메모','x':0,'y':6,'w':4,'h':2,'config':{'text':'합성 벤치마크 메모'}})
            assert api.put('/api/layout',json=layout).status_code==200
            for n in range(60):
                assert api.post('/api/tasks',json={'title':f'합성 항목 {n}','date':state['today'],'time':f'{n%24:02}:30'}).status_code==201
            for text in ['현재 메모 읽어줘','오늘 할 일 확인해줘','오늘 오후 일정 확인해줘']:
                voice=api.post('/api/voice/text',json={'request_id':'bench-'+str(len(samples)),'source':'synthetic','text':text}).json()['id']
                samples[text]=[];processing[text]=[]
                for n in range(args.iterations):
                    start=time.perf_counter()
                    response=api.post('/api/llm/requests',json={'request_id':f'bench-{voice}-{n}','voice_id':voice,
                        'expected_text_sha256':sha(text),'mode':'auto'})
                    elapsed=time.perf_counter()-start;d=response.json()
                    assert response.status_code==202 and d['status']=='succeeded',d
                    assert not d['dispatch_attempted'] and not d['assistant']['routing']['llm_called']
                    samples[text].append(elapsed*1000)
                    processing[text].append(d['assistant']['routing']['fast_path_seconds']*1000)
    assert not model.calls
    def summarize(values):
        values=sorted(values)
        return {'n':len(values),'p50_ms':round(statistics.median(values),3),
                'p95_ms':round(values[min(len(values)-1,int(len(values)*.95))],3),'max_ms':round(max(values),3)}
    result={'scope':'Synthetic TestClient HTTP/SQLite; excludes STT, network, actual V35 and actual Qwen.',
            'python':platform.python_version(),'platform':platform.system(),'llm_calls':model.calls,
            'api_including_history':{k:summarize(v) for k,v in samples.items()},
            'router_read_format':{k:summarize(v) for k,v in processing.items()}}
    out=ROOT/'artifacts/test-results';out.mkdir(parents=True,exist_ok=True)
    (out/'fast-read-latency.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
