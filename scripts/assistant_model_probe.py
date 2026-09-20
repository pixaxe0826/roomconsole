"""Optional actual-model check against V35 localhost:8090 using synthetic inputs.
Never calls a Room Hub mutation API, reads user transcripts or changes settings.
Writes only data/assistant-model-probe.json (private, excluded from Git).
Uses the same prompts/schema as the deployed application; no mock success path.
"""
from __future__ import annotations
import json,os,sys,time,urllib.request
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.assistant import detect,payload,grounded
from app.llm import LLMConfig,parse_result
from app.response_quality import assess

def main():
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    cfg=LLMConfig(enabled=True)
    cases=[('저녁메뉴 추천해 줘.','chat'),('내일 택배 보내기 할 일로 등록해 줘','parser')]
    report={'scope':'Actual configured local Qwen; synthetic prompts only; no tasks executed. Not a comprehensive language benchmark.','at':datetime.now(timezone.utc).isoformat(),'cases':[]}
    for text,route in cases:
        rec=detect(text,report['at'],'Asia/Seoul');rec['route']=route
        body=payload(rec,cfg);row={'input':text,'route':route,'request':json.loads(body),'ok':False}
        begin=time.perf_counter()
        try:
            req=urllib.request.Request('http://127.0.0.1:8090/v1/chat/completions',data=body.encode(),headers={'Content-Type':'application/json'})
            with op.open(req,timeout=300) as res:
                raw=res.read(512*1024+1)
            if len(raw)>512*1024:raise ValueError('Oversized model response')
            value=json.loads(raw);result=parse_result(value,time.perf_counter()-begin)
            row['result']=result;row['response_seconds']=time.perf_counter()-begin
            if result.get('finish_reason')=='length':raise ValueError('Output truncated')
            answer=result.get('output') or ''
            if route=='parser':
                p=grounded(json.loads(answer),rec)
                if p.intent!='todo.create' or p.title!='택배 보내기' or p.date_ref!='내일':raise ValueError('Schema valid but wrong intent/date/title')
                row['validated_proposal']=p.model_dump();row['ok']=True
            else:
                compact=lambda s:''.join(c for c in s if c.isalnum())
                row['quality']=assess(answer,text,result.get('finish_reason'))
                row['ok']=row['quality']['ok'] and compact(answer)!=compact(text)
                row['note']='Human review is still needed; nonempty/non-echo is not semantic accuracy.'
            print(route,':', 'PASS' if row['ok'] else 'REVIEW NEEDED', flush=True)
        except Exception as exc:
            row['error']=type(exc).__name__+': '+str(exc)[:300];print(route,': FAILED / REVIEW NEEDED',flush=True)
        report['cases'].append(row)
    report['basic_checks_passed']=all(x['ok'] for x in report['cases'])
    dest=ROOT/'data/assistant-model-probe.json';dest.parent.mkdir(exist_ok=True)
    if dest.is_symlink():raise RuntimeError('Refusing output symlink')
    fd=os.open(dest,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print('Report:',dest)
    print('No task, runtime, or model settings were changed.')
    return 0 if report['basic_checks_passed'] else 1
if __name__=='__main__':raise SystemExit(main())
