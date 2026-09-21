"""Opt-in loopback llama-server compatibility probe; no Hub DB/actions/config edits.

Default: GET-only /props and /v1/models. --generate explicitly makes ONE synthetic
constrained decoding request. It does not load another model or install software.
"""
from __future__ import annotations
import argparse
import json
import os
import re
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 512 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirects are disabled')


def exchange(url, payload=None, timeout=30):
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('HUB_LLM_API_KEY')
    if key:
        headers['Authorization'] = 'Bearer ' + key
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as r:
        raw = r.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError('Response too large')
    return json.loads(raw)


def probe(base_url, model, generate=False, timeout=30):
    u=urllib.parse.urlsplit(base_url)
    if not (u.scheme=='http' and u.hostname in {'127.0.0.1','::1'} and
            u.port and 1024<=u.port<=65535 and u.port not in {8080,8088,8443,8022} and
            not u.username and not u.password and not u.query and not u.fragment and u.path.rstrip('/')=='/v1'):
        raise ValueError('Only explicit loopback llama-server /v1 URLs are supported')
    base_url=base_url.rstrip('/')
    report={'scope':'Synthetic compatibility probe, not task accuracy or V35 latency validation',
            'schema_probe':'not_run','model_match':False,'build_info':None,'repo_baseline':'b6000 / 4762ad7316dcdec20016ab5985fb46a27902204d'}
    models=exchange(base_url+'/models', timeout=timeout)
    if not isinstance(models,dict) or not isinstance(models.get('data'),list):
        raise ValueError('Invalid models response')
    report['model_match']=any(x.get('id')==model for x in models['data'] if isinstance(x,dict))
    try:
        props=exchange(base_url[:-3]+'/props', timeout=timeout)
        build=props.get('build_info') if isinstance(props,dict) else None
        report['build_info']=build if isinstance(build,str) and re.fullmatch(r'b\d+-[a-fA-F0-9]{7,40}',build) else None
    except (ValueError, OSError):
        report['build_info']=None  # Never export props/model paths/template/credentials.
    if not generate:
        return report
    if not report['model_match']:
        report['schema_probe']='not_run_model_mismatch'
        return report
    schema={'type':'object','properties':{'probe':{'const':'room-hub-widget-schema'}},
            'required':['probe'],'additionalProperties':False}
    payload={'model':model,'stream':False,'temperature':0,'max_tokens':64,
             'chat_template_kwargs':{'enable_thinking':False},
             'messages':[{'role':'system','content':'Compatibility test only. Do not perform any action. /no_think'},
                         {'role':'user','content':'Emit the word incompatible, not a JSON object.'}],
             'response_format':{'type':'json_schema','json_schema':{'name':'room_hub_probe','strict':True,'schema':schema}}}
    start=time.perf_counter()
    reply=exchange(base_url+'/chat/completions',payload,timeout)
    report['probe_ms']=round((time.perf_counter()-start)*1000,3)
    choices=reply.get('choices',[]) if isinstance(reply,dict) else []
    choice=choices[0] if isinstance(choices,list) and choices and isinstance(choices[0],dict) else {}
    message=choice.get('message') or {}
    if not isinstance(message,dict):
        message={}
    try:
        value=json.loads(message.get('content') or '')
    except (ValueError, TypeError):
        value=None
    good=(choice.get('finish_reason')=='stop' and value=={'probe':'room-hub-widget-schema'}
          and not message.get('tool_calls') and not message.get('refusal'))
    report['schema_probe']='passed' if good else 'failed'
    # Never export provider raw text, paths, or API credentials.
    report['note']='One constrained sample only; server-side validation remains mandatory.'
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:8090/v1')
    p.add_argument('--model',default='Qwen3-0.6B-Q5_K_M.gguf')
    p.add_argument('--generate',action='store_true')
    p.add_argument('--timeout',type=int,default=30)
    args=p.parse_args(argv)
    try:
        report=probe(args.base_url,args.model,args.generate,args.timeout)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'schema_probe':'failed','error_type':type(exc).__name__,'message':'연결·서버 호환성을 확인하세요. 자유 응답 재시도는 하지 않았습니다.'},ensure_ascii=False))
        return 1
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['model_match'] and report['schema_probe'] in {'not_run','passed'} else 1


if __name__=='__main__':
    raise SystemExit(main())
