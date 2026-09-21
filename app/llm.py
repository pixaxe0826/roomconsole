"""Manager-only request notebook and explicitly confirmed local assistant actions.
No model installation, shell execution, or automatic write dispatch.

One explicit request -> immutable transcript/messages/JSON snapshot -> one HTTP
attempt. No automatic retry after a failure, cancellation, or process restart.
Metrics are provider-reported tokens/timings, never guessed from text length.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import time
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import uid, utcnow
from .assistant import AssistantEngine, detect, payload as assistant_payload, dump as adump, Confirmation, PROTOCOL

from .clock_service import clock_context, aware
from .capabilities import manifest as capability_manifest
from .response_quality import assess as assess_response, safe_message as quality_message

MAX_BODY = 512 * 1024
MAX_HISTORY = 1000
MAX_PENDING = 4
ACTIVE = {'queued', 'running'}
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted', 'awaiting_confirmation', 'needs_clarification'}
DEFAULT_PROMPT = (
    '당신은 Room Hub의 입력 검토 도우미입니다. 사용자 전사문에 한국어로 짧게 답하세요. '
    '실제로 일정이나 할 일을 변경할 권한은 없습니다. 실행하지 않은 작업을 완료했다고 말하지 마세요. '
    '전사문에 없는 날짜, 이름, 정보를 지어내지 말고 불명확하면 확인이 필요하다고 답하세요.'
)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    enabled: bool = Field(default=False, strict=True)
    # Literal loopback only. No DNS, redirects, ambient proxy, cloud fallback.
    base_url: str = 'http://127.0.0.1:8090/v1'
    model: str = Field(default='Qwen3-0.6B-Q5_K_M.gguf', min_length=1, max_length=160)
    system_prompt: str = Field(default=DEFAULT_PROMPT, max_length=6000)
    include_time_context: bool = Field(default=True, strict=True)
    max_tokens: int = Field(default=256, ge=16, le=1024, strict=True)
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout_seconds: int = Field(default=180, ge=10, le=600, strict=True)
    non_thinking: bool = Field(default=True, strict=True)
    @field_validator('base_url')
    @classmethod
    def local_url(cls, value: str) -> str:
        try:
            u = urlsplit(value)
            valid = (u.scheme == 'http' and u.hostname in {'127.0.0.1', '::1'}
                     and u.username is None and u.password is None
                     and u.port is not None and 1024 <= u.port <= 65535
                     and u.port not in {8080, 8088, 8443, 8022}
                     and u.path.rstrip('/') == '/v1' and not u.query and not u.fragment)
            if not valid or any(c.isspace() for c in value):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('LLM 주소는 http://127.0.0.1:8090/v1 형식의 로컬 전용 주소여야 합니다. 기존 서비스 포트는 사용할 수 없습니다.')
        return value.rstrip('/')
    @field_validator('model')
    @classmethod
    def model_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('모델 ID의 앞뒤 공백과 제어 문자는 허용하지 않습니다.')
        return value


class LLMCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    voice_id: str = Field(min_length=1, max_length=80)
    mode: Literal['legacy','auto','chat'] = 'legacy'
    expected_text_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class LLMRetry(BaseModel):
    mode: Literal['legacy','auto','chat'] | None = None
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class LLMFailure(Exception):
    """Safe user-facing error. Never return credentials or arbitrary error body."""
    def __init__(self, message: str, code='backend_error', http_status=None):
        super().__init__(message)
        self.code, self.http_status = code, http_status


def nonnegative_int(value):
    return value if type(value) is int and 0 <= value <= 100_000_000 else None


def number(value, positive=False):
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    return float(value) if (value > 0 if positive else value >= 0) else None


def parse_result(body: Any, request_seconds: float) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get('choices'), list) or not body['choices']:
        raise LLMFailure('LLM 응답에 choices가 없습니다. Chat Completions 호환 설정을 확인하세요.', 'invalid_response')
    choice = body['choices'][0]
    if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
        raise LLMFailure('LLM 응답의 message 형식이 올바르지 않습니다.', 'invalid_response')
    msg = choice['message']; content = msg.get('content')
    # Preserve content as provided. Never strip/synthesize reasoning into final output.
    if content is not None and not isinstance(content, str):
        raise LLMFailure('문자열 형태의 LLM 출력을 지원합니다.', 'invalid_response')
    reasoning = msg.get('reasoning_content', msg.get('reasoning'))
    if reasoning is not None and not isinstance(reasoning, str): reasoning = None
    calls = msg.get('tool_calls')
    if content is None and reasoning is None and not calls and not msg.get('refusal'):
        raise LLMFailure('LLM의 출력 내용을 확인할 수 없습니다.', 'invalid_response')
    usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
    timing = body.get('timings') if isinstance(body.get('timings'), dict) else {}
    out_tokens = nonnegative_int(usage.get('completion_tokens'))
    count_source = 'usage.completion_tokens' if out_tokens is not None else None
    predicted_n = nonnegative_int(timing.get('predicted_n'))
    if out_tokens is None and predicted_n is not None:
        out_tokens, count_source = predicted_n, 'timings.predicted_n'
    prompt_tokens = nonnegative_int(usage.get('prompt_tokens'))
    gen_ms = number(timing.get('predicted_ms'), positive=True)
    reported_tps = number(timing.get('predicted_per_second'))
    # Decode TPS is not completion_tokens / total request time.
    tps = None; rate_source = None
    if reported_tps is not None and gen_ms is not None and predicted_n is not None:
        tps, rate_source = reported_tps, 'timings.predicted_per_second (backend)'
    elif gen_ms is not None and predicted_n is not None:
        tps, rate_source = predicted_n / (gen_ms / 1000), 'timings.predicted_n / (predicted_ms / 1000)'
    warnings = []
    if out_tokens is None: warnings.append('백엔드가 출력 토큰 수를 제공하지 않았습니다. 글자 수로 추정하지 않습니다.')
    if tps is None: warnings.append('백엔드 생성 시간 통계가 없어 순수 토큰 생성 속도는 확인할 수 없습니다.')
    if count_source == 'usage.completion_tokens' and predicted_n is not None and predicted_n != out_tokens:
        warnings.append('usage와 timings의 토큰 수가 다릅니다. 두 원본 값을 별도로 보관합니다.')
    finish = choice.get('finish_reason')
    if finish == 'length': warnings.append('출력 토큰 한도에 도달했습니다. 결과가 잘렸을 수 있습니다.')
    if reasoning: warnings.append('백엔드가 별도의 reasoning 내용을 반환했습니다. 최종 출력과 분리해 보관합니다.')
    if calls: warnings.append('도구 호출 응답을 받았지만 실행하지 않았습니다.')
    return {'output': content, 'reasoning': reasoning, 'tool_calls': calls,
            'refusal': msg.get('refusal'), 'response_model': str(body.get('model', ''))[:200],
            'finish_reason': finish if isinstance(finish, str) else None,
            'usage': usage, 'timings': timing, 'warnings': warnings,
            'metrics': {'request_seconds': request_seconds, 'output_tokens': out_tokens,
                'output_tokens_source': count_source, 'prompt_tokens': prompt_tokens,
                'generation_seconds': gen_ms / 1000 if gen_ms is not None else None,
                'generation_tps': tps, 'generation_tps_source': rate_source,
                'prompt_seconds': number(timing.get('prompt_ms')) / 1000 if number(timing.get('prompt_ms')) is not None else None,
                'end_to_end_tps': out_tokens / request_seconds if out_tokens is not None and request_seconds > 0 else None}}


class ChatBackend:
    """Non-streaming HTTP adapter; exact body is supplied by the immutable snapshot."""
    def __init__(self, transport=None): self.transport = transport

    async def request(self, method, url, *, body=None, timeout=10):
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        key = os.environ.get('HUB_LLM_API_KEY', '')
        if key: headers['Authorization'] = 'Bearer ' + key
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(timeout, connect=min(5, timeout))) as client:
                async with client.stream(method, url, headers=headers, content=body) as res:
                    if res.status_code != 200:
                        label = {400:'요청 설정이나 컨텍스트 한도를 확인하세요.', 401:'LLM 백엔드 인증을 확인하세요.',
                                 403:'LLM 백엔드 접근 권한을 확인하세요.', 404:'모델 ID와 API 경로를 확인하세요.',
                                 429:'LLM 백엔드가 사용 중입니다.', 503:'LLM 모델을 로딩 중이거나 서버가 준비되지 않았습니다.'}.get(res.status_code, 'LLM 백엔드 응답을 확인하세요.')
                        raise LLMFailure(f'LLM HTTP {res.status_code}: {label}', 'http_error', res.status_code)
                    chunks=[]; size=0
                    async for chunk in res.aiter_bytes():
                        size+=len(chunk)
                        if size > MAX_BODY: raise LLMFailure('LLM 응답이 512KiB 제한을 넘었습니다.', 'response_too_large')
                        chunks.append(chunk)
            raw = b''.join(chunks).decode('utf-8')
            value=json.loads(raw, parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('nonfinite')))
            return value, raw
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise LLMFailure('LLM 응답 제한 시간을 넘었습니다. 자동 재시도하지 않습니다.', 'timeout') from exc
        except httpx.RequestError as exc:
            raise LLMFailure('로컬 LLM 서버에 연결할 수 없습니다. 모델 설치와 실행 상태를 확인하세요.', 'connection_failed') from exc
        except (UnicodeError, ValueError) as exc:
            raise LLMFailure('LLM이 유효한 JSON 응답을 반환하지 않았습니다.', 'invalid_response') from exc

    async def generate(self, endpoint, request_body, timeout):
        # Outer deadline also covers a server that drips bytes forever.
        try:
            return await asyncio.wait_for(self.request('POST', endpoint, body=request_body.encode('utf-8'), timeout=timeout), timeout)
        except asyncio.TimeoutError as exc:
            raise LLMFailure('LLM 응답 제한 시간을 넘었습니다. 자동 재시도하지 않습니다.', 'timeout') from exc

    async def probe(self, cfg):
        try:
            value, _ = await asyncio.wait_for(self.request('GET', cfg.base_url+'/models', timeout=5), 5)
            if not isinstance(value, dict) or not isinstance(value.get('data'), list):
                raise LLMFailure('/v1/models 응답 형식을 확인하세요.', 'invalid_response')
            models=[v['id'] for v in value['data'] if isinstance(v, dict) and isinstance(v.get('id'), str)][:100]
            match = cfg.model in models
            return {'ok': match, 'reachable': True, 'checked_at': utcnow(), 'models': models,
                    'message': '설정한 모델 ID를 확인했습니다. 실제 생성 가능 여부는 요청 결과로 확인하세요.' if match else '서버에 접속했지만 설정한 모델 ID가 목록에 없습니다.'}
        except (LLMFailure, asyncio.TimeoutError) as exc:
            return {'ok':False,'reachable':False,'checked_at':utcnow(),'models':[],
                    'message':str(exc) if str(exc) else '모델 목록 조회 제한 시간을 넘었습니다.'}


class LLMHub:
    def __init__(self, store, changed, *, speech=None, backend=None):
        self.store, self.changed, self.speech = store, changed, speech
        self.backend = backend or ChatBackend()
        self.wake=asyncio.Event(); self.worker=None; self.active_id=None; self.call=None
        self.start_lock=asyncio.Lock(); self.probe_lock=asyncio.Lock(); self.last_probe=None
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS llm_requests (
              id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, signature TEXT NOT NULL,
              voice_id TEXT REFERENCES voice(id) ON DELETE SET NULL,
              source_voice_id TEXT NOT NULL, parent_id TEXT,
              source_text TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_meta TEXT NOT NULL,
              config_json TEXT NOT NULL, request_body TEXT NOT NULL, request_sha256 TEXT NOT NULL,
              endpoint TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', error_code TEXT,
              response_json TEXT, response_raw TEXT, http_status INTEGER,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, queued_at TEXT,
              started_at TEXT, finished_at TEXT, request_seconds REAL,
              wait_seconds REAL, dispatch_attempted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS llm_status ON llm_requests(status,created_at);
            CREATE INDEX IF NOT EXISTS llm_voice ON llm_requests(source_voice_id,created_at);
            ''')
        self.assistant = AssistantEngine(store)
        self.widget_bridge = None
        if store.get('llm_config') is None: store.set('llm_config', LLMConfig().model_dump())

    def set_widget_registry(self, registry):
        from .widget_bridge import WidgetBridge
        self.widget_bridge = WidgetBridge(self.store, self.assistant, registry)

    def config(self): return LLMConfig.model_validate(self.store.get('llm_config'))

    def status(self):
        cfg = self.config()
        with self.store.connect() as db:
            counts=dict(db.execute('SELECT status,COUNT(*) FROM llm_requests GROUP BY status').fetchall())
        return {'config':cfg.model_dump(),'counts':counts,'last_probe':self.last_probe,
                'active_id':self.active_id,'auto_execute':False,'streaming':False,
                'assistant_protocol':PROTOCOL,'capability_catalog':capability_manifest(),
                'clock':clock_context(utcnow(),self.store.get('settings')['timezone']),
                'assistant_profiles':{'chat':{'temperature':0.7,'top_p':0.8,'top_k':20,'min_p':0.0,'presence_penalty':1.0,'max_tokens_cap':128},'parser':{'temperature':0,'max_tokens_cap':256},'widget_proposal':{'temperature':0,'max_tokens_cap':64,'schema_constrained':True}},
                'widget_bridge':bool(self.widget_bridge),'widget_capability_catalog':self.widget_bridge.registry.manifest() if self.widget_bridge else None,
                'max_pending':MAX_PENDING,'max_history':MAX_HISTORY,
                'worker_running':bool(self.worker and not self.worker.done())}

    async def notify(self, event, detail=''):
        # Only record metadata/IDs in audit, not transcript/prompt/output.
        await self.changed(event,detail)

    async def configure(self, cfg):
        with self.store.connect() as db:
            active=db.execute("SELECT count(*) FROM llm_requests WHERE status IN ('queued','running')").fetchone()[0]
        if active: raise HTTPException(409, '대기·실행 중인 LLM 요청을 완료하거나 취소한 뒤 설정을 변경하세요.')
        self.store.set('llm_config', cfg.model_dump()); self.last_probe=None
        if cfg.enabled:self._ensure_worker()
        await self.notify('llm.settings_updated')
        return self.status()

    async def probe(self):
        if self.probe_lock.locked(): raise HTTPException(409, '연결 확인이 진행 중입니다.')
        async with self.probe_lock: self.last_probe=await self.backend.probe(self.config())
        return self.last_probe

    def get(self, rid):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM llm_requests WHERE id=?',(rid,)).fetchone()
        if row is None: raise HTTPException(404, 'LLM 요청 기록이 없습니다.')
        d=dict(row)
        for key in ('source_meta','config_json','response_json'):
            d[key]=json.loads(d[key]) if d[key] else None
        d['request_payload']=json.loads(d['request_body'])
        d['dispatch_attempted']=bool(d['dispatch_attempted'])
        d['assistant']=self.assistant.get(rid)
        # Idempotency implementation details do not belong to displayed request body.
        d.pop('signature'); d.pop('request_key')
        return d

    def listing(self, limit=30, offset=0, status='', voice_id='', query=''):
        where=[]; args=[]
        if status:
            if status not in ACTIVE|TERMINAL|{'prepared'}: raise HTTPException(422,'알 수 없는 상태입니다.')
            where.append('status=?'); args.append(status)
        if voice_id: where.append('source_voice_id=?');args.append(voice_id)
        if query:
            # literal search: %, _ are not wildcard controls
            where.append("source_text LIKE ? ESCAPE '\\'")
            args.append('%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        clause=' WHERE '+' AND '.join(where) if where else ''
        with self.store.connect() as db:
            total=db.execute('SELECT count(*) FROM llm_requests'+clause,args).fetchone()[0]
            rows=db.execute('SELECT id FROM llm_requests'+clause+' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        items=[]
        for row in rows:
            d=self.get(row['id']); r=d['response_json'] or {}
            items.append({k:d[k] for k in ('id','voice_id','source_voice_id','parent_id','status','created_at','updated_at','started_at','finished_at','request_seconds','wait_seconds','error','dispatch_attempted')} | {
                'source_preview':d['source_text'][:220], 'model':d['config_json']['model'],
                'output_preview':(r.get('output') or '')[:180], 'metrics':r.get('metrics',{}),
                'route':(d.get('assistant') or {}).get('route'), 'output_source':r.get('output_source','llm')})
        return {'items':items,'total':total,'limit':limit,'offset':offset}

    def make_payload(self, cfg, text, at):
        prompt=cfg.system_prompt
        if cfg.include_time_context:
            tz=self.store.get('settings')['timezone']
            stamp=datetime.fromisoformat(at).astimezone(ZoneInfo(tz)).isoformat(timespec='seconds')
            prompt += f'\n입력 기준 시각: {stamp}; 시간대: {tz}. 상대 날짜는 이 기준으로 해석하세요.'
        body={'model':cfg.model,'messages':[{'role':'system','content':prompt},{'role':'user','content':text}],
              'stream':False,'max_tokens':cfg.max_tokens,'temperature':cfg.temperature}
        if cfg.non_thinking: body['chat_template_kwargs']={'enable_thinking':False}
        return encoded(body)

    def _duplicate(self, key, signature):
        with self.store.connect() as db:
            row=db.execute('SELECT id,signature FROM llm_requests WHERE request_key=?',(key,)).fetchone()
        if not row:return None
        if row['signature']!=signature:raise HTTPException(409,'같은 요청 ID에 다른 입력을 보낼 수 없습니다.')
        return self.get(row['id']) | {'duplicate':True}

    async def submit(self, req):
        sig=sha(encoded({'voice_id':req.voice_id,'text_hash':req.expected_text_sha256,**({'mode':req.mode} if req.mode!='legacy' else {})}))
        duplicate=self._duplicate(req.request_id,sig)
        if duplicate:return duplicate
        with self.store.connect() as db:
            v=db.execute('SELECT * FROM voice WHERE id=?',(req.voice_id,)).fetchone()
            job=db.execute('SELECT status FROM speech_jobs WHERE voice_id=?',(req.voice_id,)).fetchone()
        if v is None:raise HTTPException(404,'음성 수신함 항목이 없습니다.')
        if job and job['status'] in {'queued','running'}:raise HTTPException(409,'전사가 완료된 뒤 LLM에 전송하세요.')
        if not v['text'].strip():raise HTTPException(422,'저장된 전사 텍스트가 없습니다.')
        if sha(v['text'])!=req.expected_text_sha256:raise HTTPException(409,'전사 내용이 변경되었습니다. 최신 저장 내용을 확인하고 다시 보내세요.')
        meta={k:v[k] for k in ('source','locale','kind','created_at','status')}
        return await self._insert(req.request_id,sig,v['id'],v['id'],v['text'],meta,mode=req.mode)

    async def submit_transcription(self, job_id: str, voice_id: str, text: str):
        """Submit one successful STT result through the normal auto assistant route.

        The speech job id is the idempotency key. Fast reads still stay local;
        writes still stop at confirmation; only fallback/chat reaches the model.
        """
        req = LLMCreate(request_id='speech-auto:'+job_id, voice_id=voice_id, mode='auto',
                        expected_text_sha256=sha(text))
        return await self.submit(req)

    async def retry(self,rid,req):
        prev=self.get(rid)
        if prev['status'] in ACTIVE:raise HTTPException(409,'현재 요청을 완료하거나 취소한 뒤 재요청하세요.')
        mode=req.mode or (prev.get('assistant') or {}).get('mode','legacy')
        sig=sha(encoded({'parent':rid,**({'mode':mode} if mode!='legacy' else {})}))
        duplicate=self._duplicate(req.request_id,sig)
        if duplicate:return duplicate
        # Preserve the ORIGINAL user text. Current system/settings/time become a new snapshot.
        return await self._insert(req.request_id,sig,prev['voice_id'],prev['source_voice_id'],prev['source_text'],prev['source_meta'],rid,mode=mode)

    async def _insert(self,key,sig,voice_id,source_id,text,meta,parent=None,mode='legacy'):
        async with self.start_lock:
            duplicate=self._duplicate(key,sig)
            if duplicate:return duplicate
            cfg=self.config();now=utcnow();rid=uid();body=self.make_payload(cfg,text,now)
            router_start=time.perf_counter()
            agent=detect(text,now,self.store.get('settings')['timezone'],mode)
            if self.widget_bridge:agent=self.widget_bridge.prepare(agent)
            agent['routing']['router_seconds']=time.perf_counter()-router_start
            if agent.get('widget_trace'):
                agent['widget_trace']['latency_ms']['router_ms']=agent['routing']['router_seconds']*1000
            if mode=='legacy' and not agent.get('fast_read') and not agent.get('_widget_domain_request'):agent=None
            if agent:
                agent['source_received_at']=meta.get('created_at')
                agent['reference_policy']='new request uses request-created clock; previous voice timestamp recorded separately'
                body=(self.widget_bridge.payload(agent,cfg) if agent.get('widget_bridge') and not agent.get('fast_read')
                      else assistant_payload(agent,cfg)) or encoded({'local_plan':agent.get('proposal'),'route':agent['route']})
                if agent.get('widget_bridge'):
                    with self.store.connect() as db:
                        stt=db.execute('SELECT elapsed FROM speech_jobs WHERE voice_id=?',(voice_id,)).fetchone()
                    if stt and stt['elapsed'] is not None:agent['widget_trace']['latency_ms']['stt_ms']=stt['elapsed']*1000
            local=bool(agent and agent['route'] in {'rule','clarify'})
            fast=bool(agent and agent.get('fast_read'))
            status='running' if fast else 'queued' if cfg.enabled or local else 'prepared'
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT count(*) FROM llm_requests').fetchone()[0]>=MAX_HISTORY:
                    raise HTTPException(429,'LLM 기록이 1,000개입니다. 필요한 기록을 내보내고 오래된 항목을 삭제하세요.')
                if status=='queued' and db.execute("SELECT count(*) FROM llm_requests WHERE status IN ('queued','running')").fetchone()[0]>=MAX_PENDING:
                    raise HTTPException(429,'LLM 대기열이 가득 찼습니다. 완료 후 다시 보내세요.')
                db.execute('''INSERT INTO llm_requests(id,request_key,signature,voice_id,source_voice_id,parent_id,
                  source_text,source_sha256,source_meta,config_json,request_body,request_sha256,endpoint,status,
                  created_at,updated_at,queued_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (rid,key,sig,voice_id,source_id,parent,text,sha(text),encoded(meta),encoded(cfg.model_dump()),
                   body,sha(body),cfg.base_url+'/chat/completions' if not local else 'local://room-hub',status,now,now,now if status in {'queued','running'} else None))
                if agent:self.assistant.attach(db,rid,agent,parent)
            if status=='queued':self._ensure_worker()
            self.wake.set()
        if fast:await self._execute_fast_read(rid)
        await self.notify('llm.request_created',rid)
        return self.get(rid)|{'duplicate':False}

    async def run_prepared(self,rid):
        async with self.start_lock:
            d=self.get(rid);cfg=self.config()
            if d['status'] in ACTIVE:return d
            if d['status']!='prepared':raise HTTPException(409,'미전송 요청만 실행할 수 있습니다. 기존 결과는 재요청으로 보존하세요.')
            if not cfg.enabled:raise HTTPException(409,'LLM 연결을 아직 사용하지 않습니다. 모델을 준비한 뒤 연결 설정을 켜세요.')
            saved=dict(d['config_json']);nowcfg=cfg.model_dump();saved.pop('enabled');nowcfg.pop('enabled')
            if saved!=nowcfg:raise HTTPException(409,'생성 당시의 설정과 다릅니다. 현재 설정으로 새 요청을 만드세요.')
            with self.store.connect() as db:
                if db.execute("SELECT count(*) FROM llm_requests WHERE status IN ('queued','running')").fetchone()[0]>=MAX_PENDING:raise HTTPException(429,'LLM 대기열이 가득 찼습니다.')
                db.execute("UPDATE llm_requests SET status='queued',queued_at=?,updated_at=? WHERE id=?",(utcnow(),utcnow(),rid))
            self.wake.set()
        await self.notify('llm.queued',rid);return self.get(rid)

    async def cancel(self,rid):
        if self.widget_bridge:self.widget_bridge.assert_not_executing(rid)
        d=self.get(rid)
        if d['status'] not in ACTIVE|{'prepared','awaiting_confirmation'}:return d
        # Cancellation is local: backend may not abort its computation instantly.
        with self.store.connect() as db:
            db.execute("UPDATE llm_requests SET status='cancelled',error=?,error_code='cancelled',updated_at=?,finished_at=? WHERE id=?",
                ('요청을 취소했습니다. 이미 전송된 경우 백엔드의 연산 종료 시점은 확인할 수 없습니다.',utcnow(),utcnow(),rid))
        agent=self.assistant.get(rid)
        if agent:
            agent.update(state='cancelled',final_text='요청을 취소했습니다. 할 일은 변경하지 않았습니다.')
            self.assistant.put(rid,agent)
            response=dict(d.get('response_json') or {})
            response.update(output=agent['final_text'],output_source='server',action_state='cancelled')
            with self.store.connect() as db:
                db.execute('UPDATE llm_requests SET response_json=? WHERE id=?',(encoded(response),rid))
        if self.active_id==rid and self.call:self.call.cancel()
        await self.notify('llm.cancelled',rid);self.wake.set();return self.get(rid)

    async def delete(self,rid):
        if self.get(rid)['status'] in ACTIVE:raise HTTPException(409,'실행 중인 기록은 먼저 취소하세요.')
        with self.store.connect() as db:db.execute('DELETE FROM llm_requests WHERE id=?',(rid,))
        await self.notify('llm.deleted',rid);return {'ok':True}

    def ensure_voice_deletable(self,vid):
        with self.store.connect() as db:
            active=db.execute("SELECT 1 FROM llm_requests WHERE voice_id=? AND status IN ('queued','running')",(vid,)).fetchone()
        if active:raise HTTPException(409,'연결된 LLM 요청을 완료하거나 취소한 뒤 원본을 삭제하세요.')

    async def start(self):
        # Never resend jobs that may have been consumed by a backend before a restart.
        with self.store.connect() as db:
            db.execute("UPDATE llm_requests SET status='interrupted',error=?,error_code='server_restarted',updated_at=?,finished_at=? WHERE status IN ('queued','running')",
                       ('서버가 재시작되어 요청을 중단했습니다. 자동으로 다시 보내지 않습니다.',utcnow(),utcnow()))
        self._ensure_worker()

    def _ensure_worker(self):
        if self.worker and not self.worker.done():return
        self.wake=asyncio.Event()
        self.worker=asyncio.create_task(self._loop())

    async def close(self):
        if self.call:self.call.cancel()
        if self.worker and self.worker.get_loop() is asyncio.get_running_loop():
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.worker
        with self.store.connect() as db:
            db.execute("UPDATE llm_requests SET status='interrupted',error=?,error_code='server_stopped',updated_at=?,finished_at=? WHERE status IN ('queued','running')",
                       ('서버가 종료되었습니다. 자동으로 다시 보내지 않습니다.',utcnow(),utcnow()))

    async def _loop(self):
        while True:
            with self.store.connect() as db:
                row=db.execute("SELECT id FROM llm_requests WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                self.wake.clear()
                try:await asyncio.wait_for(self.wake.wait(),1)
                except asyncio.TimeoutError:pass
                continue
            # Let an already active transcription finish before starting LLM. This
            # does not impose a mutual CPU scheduler on later arriving recordings.
            if self.speech and self.speech.active_id:
                await asyncio.sleep(.2);continue
            await self._execute(row['id'])

    async def _execute(self,rid):
        d=self.get(rid)
        if d['status']!='queued':return
        if d.get('assistant') is not None:
            await self._execute_assistant(rid,d);return
        if self.widget_bridge and self.widget_bridge.has_domain(d['source_text']):
            # Preserve the old snapshot; don't run a historical free-answer prompt
            # on a personal-service request after upgrading to the Protocol bridge.
            message='이전 방식으로 준비된 Widget 요청입니다. 현재 설정으로 새 요청을 만들어 주세요.'
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET status='needs_clarification',response_json=?,updated_at=?,finished_at=? WHERE id=?",
                    (encoded({'output':message,'output_source':'server','metrics':{},'warnings':[]}),utcnow(),utcnow(),rid))
            await self.notify('assistant.stale',rid);return
        cfg=self.config()
        if not cfg.enabled:
            with self.store.connect() as db:db.execute("UPDATE llm_requests SET status='prepared',updated_at=? WHERE id=?",(utcnow(),rid))
            return
        saved=LLMConfig.model_validate(d['config_json'])
        # Endpoint is always revalidated even if DB edited externally.
        endpoint=saved.base_url+'/chat/completions'
        if endpoint!=d['endpoint'] or sha(d['request_body'])!=d['request_sha256']:
            with self.store.connect() as db:db.execute("UPDATE llm_requests SET status='failed',error=?,error_code='snapshot_invalid',updated_at=? WHERE id=?",('요청 스냅샷 무결성을 확인할 수 없습니다.',utcnow(),rid))
            await self.notify('llm.failed',rid);return
        stamp=utcnow()
        wait=max(0,(datetime.fromisoformat(stamp)-datetime.fromisoformat(d['queued_at'])).total_seconds())
        with self.store.connect() as db:
            db.execute("UPDATE llm_requests SET status='running',started_at=?,updated_at=?,wait_seconds=?,dispatch_attempted=0 WHERE id=?",(stamp,stamp,wait,rid))
        self.active_id=rid
        await self.notify('llm.running',rid)
        if self.get(rid)['status']!='running':
            self.active_id=None;return
        stamp=utcnow();start=time.perf_counter()
        with self.store.connect() as db:db.execute('UPDATE llm_requests SET started_at=?,dispatch_attempted=1 WHERE id=?',(stamp,rid))
        self.call=asyncio.create_task(self.backend.generate(endpoint,d['request_body'],saved.timeout_seconds))
        try:
            # The public widget index contains dispatch attempts only. Notify
            # after this flag is persisted, so a long request appears promptly.
            await self.notify('llm.dispatched',rid)
            body,raw=await self.call;elapsed=time.perf_counter()-start
            result=parse_result(body,elapsed)
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET status='succeeded',response_json=?,response_raw=?,http_status=200,request_seconds=?,updated_at=?,finished_at=? WHERE id=? AND status='running'",
                           (encoded(result),raw,elapsed,utcnow(),utcnow(),rid))
        except asyncio.CancelledError:
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET request_seconds=?,updated_at=? WHERE id=?",(time.perf_counter()-start,utcnow(),rid))
            if asyncio.current_task().cancelling():raise
        except LLMFailure as exc:
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET status='failed',error=?,error_code=?,http_status=?,request_seconds=?,updated_at=?,finished_at=? WHERE id=? AND status='running'",
                    (str(exc),exc.code,exc.http_status,time.perf_counter()-start,utcnow(),utcnow(),rid))
        except Exception:
            # Do not persist arbitrary exceptions: may contain local paths/keys/body.
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET status='failed',error=?,error_code='internal_error',request_seconds=?,updated_at=?,finished_at=? WHERE id=? AND status='running'",
                    ('LLM 응답 처리 중 오류가 발생했습니다. 자동 재시도하지 않습니다.',time.perf_counter()-start,utcnow(),utcnow(),rid))
        finally:
            self.call=None;self.active_id=None
        await self.notify('llm.finished',rid)


    async def confirm_action(self,rid,body):
        record=self.assistant.get(rid) or {}
        result=(await self.widget_bridge.confirm(rid,body.preview_sha256)
                if self.widget_bridge and record.get('widget_bridge') else self.assistant.confirm(rid,body.preview_sha256))
        # The mutation + receipt already committed before notification. A failed
        # socket does not roll back or repeat the mutation.
        outcome=(result.get('receipt') or {}).get('protocol_state')
        event=('assistant.confirmation_uncertain' if outcome in {'reserved','uncertain'} else
               'assistant.executed' if result['changed'] else 'assistant.already_executed')
        await self.notify(event,rid)
        return self.get(rid)|{'execution':result}

    async def _execute_fast_read(self,rid):
        """Finish bounded reads independently of the model/speech worker.

        Do not touch active_id/call: a concurrent model job owns those handles.
        Business data is read only; history and audit remain the existing store.
        """
        start=time.perf_counter();started=utcnow();d=self.get(rid);record=d['assistant']
        with self.store.connect() as db:
            db.execute('UPDATE llm_requests SET started_at=?,updated_at=?,wait_seconds=0 WHERE id=?',
                       (started,started,rid))
        try:
            if sha(d['request_body'])!=d['request_sha256']:
                raise LLMFailure('저장된 조회 계획의 무결성을 확인하지 못했습니다.','snapshot_invalid')
            record['execution_started_at']=started
            record=(await self.widget_bridge.fast_read(rid,record) if self.widget_bridge and record.get('widget_bridge')
                    else self.assistant.stage(rid,record,record['proposal']))
            elapsed=time.perf_counter()-start
            record['routing']['fast_path_seconds']=elapsed+record['routing']['router_seconds']
            if record.get('widget_trace'):record['widget_trace']['latency_ms']['total_ms']=record['routing']['fast_path_seconds']*1000
            self.assistant.put(rid,record)
            result={'output':record['final_text'],'output_source':'server','action_state':record['state'],
                    'metrics':{},'warnings':[],'assistant_seconds':elapsed}
            with self.store.connect() as db:
                db.execute('UPDATE llm_requests SET status=?,response_json=?,request_seconds=?,finished_at=?,updated_at=?,error=?,error_code=? WHERE id=?',
                           (record['state'],encoded(result),elapsed,utcnow(),utcnow(),
                            record['final_text'] if record['state']=='failed' else '',
                            'source_read_failed' if record['state']=='failed' else None,rid))
        except Exception:
            # A DB/service error is not an empty list or an excuse for LLM guessing.
            record.update(state='failed',validation='source_read_failed',final_text=None)
            self.assistant.put(rid,record)
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET status='failed',error='저장된 데이터를 읽지 못했습니다. 서버 상태를 확인해 주세요.',error_code='source_read_failed',finished_at=?,updated_at=? WHERE id=?",
                           (utcnow(),utcnow(),rid))

    async def _execute_assistant(self,rid,d):
        record=d['assistant'];saved=LLMConfig.model_validate(d['config_json'])
        if sha(d['request_body'])!=d['request_sha256']:
            with self.store.connect() as db:db.execute("UPDATE llm_requests SET status='failed',error='요청 무결성 오류',updated_at=? WHERE id=?",(utcnow(),rid))
            await self.notify('llm.failed',rid);return
        started=utcnow();start=time.perf_counter();self.active_id=rid
        result={'output':None,'output_source':'server','metrics':{},'warnings':[]}
        with self.store.connect() as db:
            db.execute("UPDATE llm_requests SET status='running',started_at=?,updated_at=?,wait_seconds=? WHERE id=?",
                (started,started,max(0,(datetime.fromisoformat(started)-datetime.fromisoformat(d['queued_at'])).total_seconds()),rid))
        await self.notify('assistant.running',rid)
        try:
            if self.get(rid)['status']!='running':return
            record['execution_started_at']=started
            # Frozen snapshots are never silently rebuilt. Old pending requests and
            # date rollover require an explicit new request, including chat context.
            stale=False
            if record.get('protocol')!=PROTOCOL:
                stale=True
            elif self.widget_bridge and (record['mode']=='auto' or self.widget_bridge.has_domain(record['raw'])) and record['route'] in {'parser','chat'} and not record.get('widget_bridge'):
                stale=True
            else:
                now=aware(started,record['timezone']);ref=aware(record['reference_at'],record['timezone'])
                if self.store.get('settings')['timezone']!=record['timezone']:stale=True
                if (record.get('proposal') or {}).get('intent')!='time.query':
                    stale |= abs((now-ref).total_seconds())>900 or now.date()!=ref.date()
            if stale:
                record.update(route='clarify',state='needs_clarification',origin='server',validation='stale_request',
                    final_text='요청의 기준 날짜·시간대 또는 처리 규칙이 바뀌었습니다. 현재 설정으로 새 요청을 만들어 주세요. 실행하지 않았습니다.')
                self.assistant.put(rid,record)
            if record['route'] in {'parser','chat'}:
                if not self.config().enabled:
                    with self.store.connect() as db:db.execute("UPDATE llm_requests SET status='prepared',updated_at=? WHERE id=?",(utcnow(),rid))
                    return
                endpoint=saved.base_url+'/chat/completions'
                if endpoint!=d['endpoint']:raise LLMFailure('저장된 엔드포인트가 다릅니다.','snapshot_invalid')
                call={'purpose':record['route'],'endpoint':endpoint,'request_payload':d['request_payload'],
                      'request_sha256':d['request_sha256'],'started_at':utcnow(),'result':None}
                record['calls']=[call];record.setdefault('routing',{})['llm_called']=True;self.assistant.put(rid,record)
                with self.store.connect() as db:db.execute('UPDATE llm_requests SET dispatch_attempted=1 WHERE id=?',(rid,))
                call_start=time.perf_counter()
                self.call=asyncio.create_task(self.backend.generate(endpoint,d['request_body'],saved.timeout_seconds))
                await self.notify('llm.dispatched',rid)
                body,raw=await self.call
                elapsed=time.perf_counter()-call_start;result=parse_result(body,elapsed)
                call.update(result=json.loads(encoded(result)),response_raw=raw,finished_at=utcnow(),seconds=elapsed)
                record['calls']=[call]
                record.setdefault('routing',{})['llm_seconds']=elapsed
                with self.store.connect() as db:db.execute('UPDATE llm_requests SET response_raw=?,http_status=200 WHERE id=?',(raw,rid))
                if record.get('widget_bridge'):
                    record=await self.widget_bridge.stage(rid,record,result)
                    result['parser_output']=result.get('output');result['output_source']='server'
                elif record['route']=='chat':
                    output=result.get('output')
                    if not output:raise LLMFailure('최종 텍스트가 없습니다. 모델 응답을 확인하세요.','empty_output')
                    check=assess_response(output,record['raw'],result.get('finish_reason'))
                    record['quality']=check
                    result['quality']=check
                    if check['ok']:
                        record.update(state='succeeded',final_text=output,validation='chat_only',origin='llm')
                        result['output_source']='llm'
                    else:
                        # Exact rejected output remains ONLY in manager call/raw logs.
                        # Display projection gets this safe status, never a hallucinated repair.
                        record.update(state='needs_clarification',final_text=quality_message(check),validation='output_quality_rejected',origin='server')
                        result['output_source']='server'
                        result['warnings'].append('답변 품질 검사: '+', '.join(check['issues'])+'. 원문은 관리자 호출 기록에만 보존했습니다. 자동 재시도하지 않습니다.')
                    self.assistant.put(rid,record)
                else:
                    # Invalid JSON, truncation, or semantics must never become an action.
                    try:
                        if result.get('finish_reason')!='stop':raise ValueError('분류 응답이 완전하지 않습니다.')
                        parsed=json.loads(result.get('output') or '')
                        record=self.assistant.stage(rid,record,parsed)
                    except (ValueError,TypeError):
                        record.update(state='needs_clarification',validation='invalid_model_proposal',final_text='명령 분석 결과를 검증하지 못했습니다. 한 작업의 날짜와 제목을 명확히 다시 말해 주세요.')
                        self.assistant.put(rid,record)
                    result['parser_output']=result.get('output');result['output_source']='server'
            elif record['route']=='rule':
                record=self.assistant.stage(rid,record,record['proposal'])
            # 'clarify' already contains a safe explanation, with no model call.
            if self.get(rid)['status']!='running':return
            result['output']=record['final_text'];result['action_state']=record['state']
            result['output_source']=record['origin'];result['assistant_seconds']=time.perf_counter()-start
            if record.get('widget_trace'):
                record['widget_trace']['latency_ms']['total_ms']=result['assistant_seconds']*1000+record['routing']['router_seconds']*1000
                self.assistant.put(rid,record)
            with self.store.connect() as db:
                db.execute("UPDATE llm_requests SET status=?,response_json=?,request_seconds=?,updated_at=?,finished_at=?,error=?,error_code=? WHERE id=? AND status='running'",
                   (record['state'],encoded(result),result['assistant_seconds'],utcnow(),utcnow(),
                    record['final_text'] if record['state']=='failed' else '',
                    'adapter_error' if record['state']=='failed' else None,rid))
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():raise
        except LLMFailure as exc:
            with self.store.connect() as db:db.execute("UPDATE llm_requests SET status='failed',error=?,error_code=?,request_seconds=?,updated_at=?,finished_at=? WHERE id=? AND status='running'",
                    (str(exc),exc.code,time.perf_counter()-start,utcnow(),utcnow(),rid))
        except Exception:
            with self.store.connect() as db:db.execute("UPDATE llm_requests SET status='failed',error='요청 처리 오류입니다. 작업은 자동 재실행하지 않습니다.',error_code='assistant_error',updated_at=?,finished_at=? WHERE id=? AND status='running'",(utcnow(),utcnow(),rid))
        finally:
            self.call=None;self.active_id=None
        await self.notify('assistant.finished',rid)
