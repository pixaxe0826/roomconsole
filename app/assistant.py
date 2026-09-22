"""Room Hub assistant v2: deterministic reads, constrained proposals, explicit writes.

Model text is never SQL, code, authority, or an execution result. All writes need
an admin confirmation bound to a frozen target/version snapshot. Effects and
receipts commit in one SQLite transaction; request-family IDs prevent retries
from applying an already executed mutation twice. No iPad write permission is
added by this module. Old LLM notebook entries remain immutable/legacy.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, ValidationError
from .fast_reads import match_read, execute_read, INTENT_NAMES
from .models import TaskCreate
from .store import uid, utcnow

from .clock_service import clock_context, resolve_day, resolve_range, prefix_date, date_evidence, aware
from .capabilities import READS, WRITES, INTENTS, require, manifest as capability_manifest, parser_description
from .command_semantics import known_read, is_personal_task_request, unsupported_personal, source_constraints, match_model_date, status_evidence, read_status

PROTOCOL = 'room-assistant-2'
DAY = r'(?:\d{4}-\d{2}-\d{2}|\d{1,2}월\s*\d{1,2}일|오늘|내일|모레|어제|(?:이번\s*주|다음\s*주)?\s*[월화수목금토일]요일)'
CHAT_PROMPT = ('한국어로 질문에 직접 답하는 개인 비서다. 입력을 되풀이하거나 확인하겠다는 약속으로 끝내지 마라. '
               '추천 요청에는 구체적 선택지와 짧은 이유를 제시하라. 불명확한 요청은 한 가지 질문으로 확인하라. '
               '할 일 DB나 실시간 정보는 이 대화에 제공되지 않았다. 조회·변경을 했다고 주장하지 마라.')
PARSER_PROMPT = ('한국어 Room Hub 명령을 JSON으로 분류한다. 실행·답변하지 마라. '
                 '사용 가능: '+parser_description()+'. '
                 'date_ref는 원문 날짜, title은 원문 제목. date_ref/title/time이 없거나 모르면 JSON null. '
                 'time은 HH:MM 또는 null만 사용. unknown 문자열을 슬롯에 쓰지 마라. '
                 '남은/미완료=pending, 완료된=completed, 그 외 all. '
                 'scope는 명시적 전체 변경만 all, 그 외 one. '
                 '날짜·조건을 지어내지 마라. 부정·모호·미지원은 intent만 unknown. JSON만 출력.')


def dump(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(x):
    return hashlib.sha256((x if isinstance(x, str) else dump(x)).encode()).hexdigest()


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    intent: Literal['memo.read','todo.create','todo.list','todo.complete','todo.uncomplete','todo.delete',
                    'calendar.query','time.query','weather.query','chat.general','unknown']
    date_ref: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=240)
    time: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    status: Literal['all','pending','completed'] = 'all'
    scope: Literal['one','all'] = 'one'

    @field_validator('time', 'date_ref', mode='before')
    @classmethod
    def missing_optional_slot(cls, value):
        # Missing is null, not a fabricated clock/date. Other invalid values
        # still fail strict validation and become a normal clarification.
        if isinstance(value, str) and value.strip().casefold() in {'', 'unknown', 'none', 'null', 'n/a'}:
            return None
        return value


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    preview_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def schema():
    # Flat/short schema is deliberately small for a 1024-token local model.
    return {'type':'object','properties':{
        'intent':{'type':'string','enum':[i for i in INTENTS if i != 'memo.read']},
        'date_ref':{'type':['string','null']}, 'title':{'type':['string','null']},
        'time':{'type':['string','null'],'pattern':r'^([01]\d|2[0-3]):[0-5]\d$'},
        'status':{'type':'string','enum':['all','pending','completed']},
        'scope':{'type':'string','enum':['one','all']}},
        'required':['intent','date_ref','title','time','status','scope'],'additionalProperties':False}


def normalize(raw):
    text = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', raw)).strip()
    # Only presentation punctuation following a recognizable request ending is
    # removed. Questions, quotations, negation and titles are not globally stripped.
    candidate = re.sub(r'[?？!！.。]+$', '', text).rstrip()
    command_end = re.search(r'(?:해|해줘|해 줘|해주세요|해 주세요|해줘요|보여줘|보여 줘|알려줘|알려 줘|추가|삭제|완료)(?:요)?$', candidate)
    if command_end and not re.search(r'["“”‘’\'`]|(?:하지\s*마|하지\s*말|하지\s*않|안\s*해|말고)', candidate):
        return candidate, ['STT 문장 끝 문장부호 정리'] if candidate != text else []
    return text, []


def resolve_date(ref, at, tz):
    return resolve_day(ref, at, tz)


def parse_clock(text):
    """Return time at the start, remaining title, ambiguity. Never guess AM/PM."""
    m=re.match(r'(?:(오전|오후|아침|저녁|밤|낮)\s*)?(\d{1,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)\s*(?:시|:)(?:\s*(\d{1,2})\s*분?|\s*(반))?\s*(?:에\s*)?',text)
    if not m:return None,text,None
    values={'한':1,'두':2,'세':3,'네':4,'다섯':5,'여섯':6,'일곱':7,'여덟':8,'아홉':9,'열':10,'열한':11,'열두':12}
    h=values.get(m[2],int(m[2]) if m[2].isdigit() else 0);minutes=30 if m[4] else int(m[3] or 0)
    if m[1] and h>12: return None,text,'오전·오후와 시각이 맞지 않습니다.'
    if m[1] in {'오후','저녁','밤','낮'}:h=(h%12)+12
    elif m[1] in {'오전','아침'}:h%=12
    elif h<=12 and ':' not in m[0]: return None,text,'오전인지 오후인지 함께 말해 주세요.'
    if not 0<=h<=23 or not 0<=minutes<=59:return None,text,'유효한 시간을 말해 주세요.'
    return f'{h:02}:{minutes:02}',text[m.end():].strip(),None


def ambiguous(text):
    # Do not turn negated, conditional, quoted, repeated, or chained commands
    # into an unconditional mutation. Structured parser is not a bypass.
    if re.search(r'하지\s*마|하지\s*말|하지\s*않|안\s*해|말고|라면|한다면|하면|했으면|할까|해도\s*될|할\s*수\s*있|않아|추가한\s*거|추가했',text):return True
    if re.search(r'["“”‘’\'`]',text):return True
    if re.search(r'(?:오늘|내일|모레|어제|\d{1,2}일)\s*(?:부터|까지|이나|또는)',text):return True
    if re.search(r'매일|매주|매월|반복|마다|추가하고|완료하고|삭제하고|하고\s*(?:나서|그리고)|;|\n',text):return True
    return False


def _detect(raw, at, tz, mode='auto'):
    normalized, notes=normalize(raw)
    s=re.sub(r'[?？!！.。]+$','',normalized).strip()
    base={'protocol':PROTOCOL,'mode':mode,'raw':raw,'normalized':normalized,'normalization_notes':notes,
          'reference_at':at,'timezone':tz,'calls':[],'route':'chat','proposal':None,'validation':None,
          'state':'new','final_text':None,'origin':'llm','action_key':None,
          'time_context':clock_context(at,tz),'capability_version':1}
    # Clear read requests win even if an old client selected chat/legacy mode.
    # Unknown inputs and all writes still follow the existing mode contract.
    try:
        plan=match_read(s,at,tz)
        if plan:
            return base|{'route':'rule','origin':'server','proposal':plan.proposal(),'fast_read':plan.data()}
    except ValueError as exc:
        return base|{'route':'clarify','state':'needs_clarification','origin':'server','final_text':str(exc)}
    if mode=='chat':return base
    unavailable=unsupported_personal(s)
    if unavailable:
        return base|{'route':'clarify','state':'needs_clarification','origin':'server','final_text':unavailable}
    try:
        read=known_read(s,at,tz)
        if read:
            return base|{'route':'rule','proposal':Proposal(**read).model_dump(),'origin':'server',
                         'constraints':source_constraints(s,at,tz,read=True)}
    except ValueError as exc:
        return base|{'route':'clarify','state':'needs_clarification','origin':'server','final_text':str(exc)}
    domain=is_personal_task_request(s)
    if domain and (len(raw)>1500 or ambiguous(s)):
        return base|{'route':'clarify','state':'needs_clarification','origin':'server',
          'final_text':'한 번에 한 요청을 명확하게 말해 주세요. 부정·가정·인용은 실행하지 않습니다. 반복 등록은 관리자 할 일 화면에서 설정해 주세요.'}
    # Strip only an explicit relative/absolute date prefix for deterministic rules.
    day,rest=prefix_date(s)
    dm=bool(day)
    if day and rest!=s:
        base['normalization_notes']=base['normalization_notes']+['날짜 경계와 문장부호 분리']
    # Unknown/invalid date or unsupported date ranges must not fall into chat.
    if domain:
        try: source_constraints(s,at,tz)
        except ValueError as exc:
            return base|{'route':'clarify','state':'needs_clarification','origin':'server','final_text':str(exc)}
    p=None
    create=re.fullmatch(r'할\s*일(?:\s*목록)?에\s+(.+?)\s*(?:추가|등록)(?:해(?:\s*줘|\s*주세요|줘요)?|해요|해주세요)?',rest)
    if create:
        tm,title,error=parse_clock(create[1].strip())
        if error:return base|{'route':'clarify','state':'needs_clarification','origin':'server','final_text':error}
        p=Proposal(intent='todo.create',date_ref=day,title=title,time=tm)
    if p is None:
        mutation=re.fullmatch(r'(.+?)\s*(완료\s*취소|미완료(?:로)?|완료|삭제|지워)(?:\s*처리)?(?:해(?:\s*줘|\s*주세요|줘요)?|줘|주세요|해요|해주세요)?',rest)
        if mutation:
            target=mutation[1].strip();verb=re.sub(r'\s+','',mutation[2]);bulk=bool(re.fullmatch(r'(?:(?:남은|모든|전체)\s*)?할\s*일(?:을)?\s*(?:전부|모두|다)?',target) and re.search(r'모든|전체|전부|모두|다$',target))
            title=None if bulk else re.sub(r'(?:을|를)$','',re.sub(r'^할\s*일\s+','',target)).strip()
            p=Proposal(intent='todo.uncomplete' if verb.startswith(('완료취소','미완료')) else 'todo.complete' if verb=='완료' else 'todo.delete',date_ref=day,title=title,scope='all' if bulk else 'one')
    if p is None and re.search(r'(몇\s*시|현재\s*시간|지금\s*시간|오늘\s*날짜|며칠|무슨\s*요일)',s):p=Proposal(intent='time.query',date_ref=day)
    if p is None and re.fullmatch(r'(?:(?:오늘|내일|모레)\s*)?(?:현재\s*)?날씨(?:를|가)?\s*(?:어때|알려\s*줘|알려\s*주세요|보여\s*줘|확인해\s*줘)(?:요)?',s):p=Proposal(intent='weather.query',date_ref=day)
    if p is None and re.fullmatch(r'(?:오늘\s*)?(?:비|눈)\s*(?:와|오나|올까|오나요)',s):p=Proposal(intent='weather.query',date_ref='오늘' if '오늘' in s else None)
    # Calendar reads must match the anchored grammar above; do not drop filters.
    if p:
        return base|{'route':'rule','proposal':p.model_dump(),'origin':'server'}
    if domain:
        if re.search(r'남은\s*애',s):
            return base|{'route':'clarify','state':'needs_clarification','origin':'server','final_text':'할 일 목록을 묻는 요청인지 확인해 주세요. 불확실한 전사를 임의로 고쳐 조회하지 않았습니다.'}
        if re.search(r'뭐\s*해야|무엇을?\s*해야|그\s*(?:날|일|거)|나머지',s) and not day:
            return base|{'route':'clarify','state':'needs_clarification','origin':'server',
                         'final_text':'어느 날짜의 할 일을 볼까요? 오늘·내일 또는 정확한 날짜로 다시 요청하세요.'}
        return base|{'route':'parser','origin':'server'}
    return base


def detect(raw, at, tz, mode='auto'):
    start=time.perf_counter()
    record=_detect(raw,at,tz,mode)
    plan=record.get('fast_read')
    route='FAST_PATH' if plan else 'LLM_FALLBACK' if record['route'] in {'parser','chat'} else 'EXISTING_RULE'
    record['routing']={'route':route,'route_reason':plan['pattern'] if plan else record['route'],
                       'resolved_intent':INTENT_NAMES.get(plan['intent']) if plan else (record.get('proposal') or {}).get('intent'),
                       'resolved_context':{},'source_of_truth':None,'llm_called':False,
                       'router_seconds':time.perf_counter()-start,'llm_seconds':0.0}
    return record


def payload(record, cfg):
    route=record['route']
    if route in {'rule','clarify'}:return None
    parser=route=='parser'
    prompt=PARSER_PROMPT if parser else CHAT_PROMPT
    ctx=record.get('time_context') or clock_context(record['reference_at'],record['timezone'])
    # Auto mode time is part of the command contract, not optional global chat prefs.
    if cfg.include_time_context or record.get('mode')=='auto':
        prompt+=f'\n기준 {ctx["local_at"]} {ctx["timezone"]}. 오늘={ctx["today"]}, 내일={ctx["tomorrow"]}.'
    result={'model':cfg.model,'messages':[{'role':'system','content':prompt},{'role':'user','content':record['normalized'] if parser else record['raw']}],
            'stream':False,'max_tokens':min(cfg.max_tokens,256 if parser else 128),
            'temperature':0 if parser else 0.7,
            'chat_template_kwargs':{'enable_thinking':False}}
    if parser:
        result['response_format']={'type':'json_schema','json_schema':{'name':'room_intent','strict':True,'schema':schema()}}
    else:
        result.update(top_p=0.8,top_k=20,min_p=0.0,presence_penalty=1.0)
    return dump(result)


def grounded(proposal, record):
    """Server allowlist + source grounding, even for schema-valid model output."""
    p=Proposal.model_validate(proposal);s=record['normalized'];flat=re.sub(r'\s+','',s)
    if p.intent=='memo.read':
        raise ValueError('메모 읽기는 서버의 명확한 읽기 전용 분기에서만 처리합니다. 메모를 읽어 달라고 다시 요청하세요.')
    if p.intent in WRITES:
        if ambiguous(s):raise ValueError('부정·조건·반복·인용 요청은 실행하지 않습니다. 한 작업으로 다시 말해 주세요.')
        evidence={'todo.create':r'추가|등록','todo.complete':r'완료','todo.uncomplete':r'완료\s*취소|미완료','todo.delete':r'삭제|지워'}
        if not re.search(evidence[p.intent],s):raise ValueError('원문에서 이 변경 요청을 확인할 수 없습니다.')
        if p.intent=='todo.complete' and re.search(r'취소|미완료',s):raise ValueError('완료와 완료 취소가 불명확합니다.')
        if p.title and re.sub(r'\s+','',p.title) not in flat:raise ValueError('할 일 제목이 원문과 다릅니다. 정확한 제목을 다시 말해 주세요.')
        if p.scope=='all' and not re.search(r'전체|전부|모두|모든|다\s*(?:완료|삭제|지워|미완료)',s):raise ValueError('모든 항목 변경이라는 명시적 요청이 없습니다.')
    if p.intent not in {'unknown','chat.general'}:
        require(p.intent)
    if p.intent in READS or p.intent in WRITES:
        if p.intent in {'todo.list','calendar.query'}:
            if not is_personal_task_request(s): raise ValueError('원문에서 할 일 조회 요청을 확인하지 못했습니다.')
            if re.search(r'오전|오후|아침|저녁|밤|낮|\d+\s*시',s):
                raise ValueError('시간대 필터를 안전하게 해석하지 못했습니다. 오늘 오전/오후 일정처럼 다시 요청하세요.')
            if ambiguous(s) and known_read(s,record['reference_at'],record['timezone']) is None:raise ValueError('부정·조건·반복·인용 요청의 범위가 불명확합니다. 한 요청으로 다시 말해 주세요.')
            if re.search(r'추가|등록|삭제|지워|완료\s*(?:처리|해)|미완료로',s):raise ValueError('변경 요청을 목록 조회로 바꿀 수 없습니다.')
            if re.search(r'제외|빼고|말고|그중|중에서|카테고리|우선순위|제목|관련|만\s*(?:보여|알려|확인)|부터|까지',s):
                raise ValueError('현재 조회는 날짜와 완료 상태를 지원합니다. 추가 필터나 여러 날짜는 관리자 목록에서 확인하세요.')
            expected=status_evidence(s)
            if expected and p.status!=expected:
                raise ValueError('원문의 완료/미완료 조건과 분석 결과가 다릅니다. 작업은 실행하지 않았습니다.')
            # Approved Assistant default, not a change to UI/DB query defaults.
            wanted = read_status(s, 'calendar' if p.intent == 'calendar.query' else 'todo')
            if not expected and p.status not in {wanted, 'all'}:raise ValueError('원문에 없는 완료 상태 필터를 제안했습니다.')
            p=p.model_copy(update={'status':wanted})
            if p.title or p.time or p.scope!='one':raise ValueError('조회에 원문에 없는 대상/시간/범위가 포함되었습니다.')
        if p.intent=='weather.query' and not re.search(r'날씨|비|눈|기온|온도|강수',s):raise ValueError('날씨 조회 근거가 없습니다.')
        if p.intent=='time.query' and not re.search(r'시간|몇\s*시|날짜|며칠|요일',s):raise ValueError('시각 조회 근거가 없습니다.')
        if p.intent!='time.query' or p.date_ref or date_evidence(s):
            canonical, resolved=match_model_date(p.date_ref,s,record['reference_at'],record['timezone'],write=p.intent in WRITES)
            p=p.model_copy(update={'date_ref':canonical})
            record['resolved_date']=resolved
            if p.intent in WRITES and resolved['start']!=resolved['end']:
                raise ValueError('변경은 한 날짜만 지원합니다. 날짜 범위는 나누어 요청하세요.')
            if p.intent in WRITES and p.scope=='all':
                expected=status_evidence(s)
                if expected and p.status not in {expected,'all'}:raise ValueError('일괄 변경의 완료 조건이 원문과 다릅니다.')
                if expected:p=p.model_copy(update={'status':expected})
                elif p.status!='all':raise ValueError('원문에 없는 일괄 필터를 제안했습니다.')
    if p.intent in {'todo.create','todo.complete','todo.uncomplete','todo.delete'} and p.scope=='one' and not p.title:
        raise ValueError('변경할 할 일의 제목을 정확히 말해 주세요.')
    if p.intent=='todo.create' and p.time is None and re.search(r'(?:\d{1,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열한|열두|열)\s*(?:시|:)',s):
        raise ValueError('원문 시각이 분석 결과에서 누락되었습니다. 오전·오후와 시간을 다시 확인해 주세요.')
    # Time returned by the model must match an actual time expression in source.
    if p.time:
        found=False
        for m in re.finditer(r'(?:오전|오후|아침|저녁|밤|낮)?\s*(?:\d{1,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열한|열두|열)\s*(?:시|:)',s):
            tm,_,err=parse_clock(s[m.start():]);found |= tm==p.time and not err
        if not found:raise ValueError('시간을 원문과 일치시킬 수 없습니다. 오전·오후와 시간을 다시 말해 주세요.')
    return p


class AssistantEngine:
    def __init__(self,store):
        self.store=store
        self.capabilities=capability_manifest()
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS assistant_runs(
                request_id TEXT PRIMARY KEY REFERENCES llm_requests(id) ON DELETE CASCADE,
                record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_effects(
                action_key TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                receipt_json TEXT NOT NULL, created_at TEXT NOT NULL);''')

    def get(self,rid,db=None):
        if db is None:
            with self.store.connect() as db:return self.get(rid,db)
        row=db.execute('SELECT record_json FROM assistant_runs WHERE request_id=?',(rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self,rid,record,db=None):
        if db is None:
            with self.store.connect() as db:self.put(rid,record,db)
            return
        db.execute('INSERT INTO assistant_runs VALUES(?,?) ON CONFLICT(request_id) DO UPDATE SET record_json=excluded.record_json',(rid,dump(record)))

    def attach(self,db,rid,record,parent=None):
        previous=self.get(parent,db) if parent else None
        record['action_key']=(previous or {}).get('action_key') or rid
        self.put(rid,record,db)

    def stage(self,rid,record,proposal):
        """Run a read or prepare an immutable write preview; does not execute writes."""
        try:
            if not isinstance(proposal,dict):raise ValueError('명령 분석 결과는 JSON 객체여야 합니다.')
            record['executed_at']=utcnow()
            if record.get('protocol')==PROTOCOL:
                settings=self.store.get('settings')
                if settings['timezone']!=record['timezone']:
                    raise ValueError('요청 후 시간대가 바뀌었습니다. 현재 설정으로 새 요청을 만드세요.')
                # A queued/prepared task must not silently reinterpret today after midnight.
                delta=(aware(record['executed_at'],record['timezone'])-aware(record['reference_at'],record['timezone'])).total_seconds()
                if proposal.get('intent')!='time.query' and (abs(delta)>900 or aware(record['reference_at'],record['timezone']).date()!=aware(record['executed_at'],record['timezone']).date()):
                    raise ValueError('요청 기준 시각이 오래되었거나 날짜가 바뀌었습니다. 현재 시각으로 새 요청을 만드세요.')
            if record.get('fast_read'):
                plan=match_read(record['normalized'],record['reference_at'],record['timezone'])
                if plan is None or plan.data()!=record['fast_read'] or plan.proposal()!=proposal:
                    raise ValueError('조회 계획이 원문과 다릅니다. 새 요청으로 다시 확인해 주세요.')
                snapshot,text,context=execute_read(self.store,plan,record['reference_at'],record['timezone'])
                record.update(state='succeeded',validation='server_read',proposal=plan.proposal(),
                              capability=require(plan.intent).public(),tool_result=snapshot,final_text=text)
                record['routing'].update(resolved_context=context,source_of_truth=snapshot['source'],llm_called=False)
                if plan.intent!='memo.read':
                    record['resolved_date']=resolve_range(plan.date_ref,record['reference_at'],record['timezone']).data()
                if plan.intent=='memo.read':record['memo_read']=True
                self.put(rid,record)
                return record
            p=grounded(proposal,record)
            if p.intent in READS|WRITES:record['capability']=require(p.intent).public()
            record['proposal']=p.model_dump()
            record.setdefault('routing',{}).update(resolved_intent=INTENT_NAMES.get(p.intent,p.intent))
            if p.intent=='unknown':raise ValueError('요청을 확실히 이해하지 못했습니다. 작업 제목과 날짜를 한 문장으로 다시 말해 주세요.')
            if p.intent=='chat.general':raise ValueError('일반 대화라면 전송 모드를 일반 대화로 선택해 다시 보내 주세요.')
            if p.intent=='time.query':
                stamp=aware(utcnow(),record['timezone'])
                if p.date_ref:
                    day=date.fromisoformat(resolve_day(p.date_ref,record['reference_at'],record['timezone']))
                    if re.search(r'몇\s*시|시간',record['normalized']) and day!=stamp.date():
                        raise ValueError('그 날짜의 어떤 시간인지 알려 주세요. 예정 시각을 임의로 만들지 않습니다.')
                    if re.search(r'몇\s*시|시간',record['normalized']):
                        text=f'{stamp:%Y-%m-%d %H:%M}입니다. ({record["timezone"]}, 서버 조회 시각)'
                    else:
                        text=f'{day.isoformat()} {"월화수목금토일"[day.weekday()]}요일입니다. ({record["timezone"]}, 요청 기준 날짜)'
                else:
                    text=f'{stamp:%Y-%m-%d %H:%M}입니다. ({record["timezone"]}, 서버 조회 시각)'
                record.update(state='succeeded',validation='server_read',tool_result={'at':stamp.isoformat(),'timezone':record['timezone'],'as_of':stamp.isoformat(),'source':'server_os_clock','clock_sync_verified':False},final_text=text)
            elif p.intent=='weather.query':
                wx=self.store.get('weather');settings=self.store.get('settings')
                if not wx:raise ValueError('저장된 날씨가 없습니다. 관리자에서 위치를 설정하고 날씨를 갱신해 주세요.')
                stamp=wx.get('fetched_at','');age=None
                try:age=(datetime.now(ZoneInfo('UTC'))-datetime.fromisoformat(stamp)).total_seconds()
                except (ValueError,TypeError):pass
                current=wx.get('current') or {}
                # Weather shape is Room Hub's own cache (not an invented model answer).
                temp=wx.get('temperature',current.get('temperature_2m'));code=current.get('weather_code')
                condition={0:'맑음',1:'대체로 맑음',2:'구름 조금',3:'흐림',45:'안개',48:'안개',51:'이슬비',53:'이슬비',55:'이슬비',61:'비',63:'비',65:'강한 비',71:'눈',73:'눈',75:'많은 눈',80:'소나기',81:'소나기',82:'강한 소나기',95:'뇌우'}.get(code,f'날씨 코드 {code}' if code is not None else '상태 미제공')
                place=settings.get('location_name') or '설정 지역'
                record.update(state='succeeded',validation='server_read',tool_result={'weather':wx,'age_seconds':age},
                              final_text=f'{place}: {condition}, 기온 {temp if temp is not None else "확인 불가"}°C. 마지막 갱신 {stamp}. '+('오래된 저장값입니다.' if age is None or age>settings['weather_interval_minutes']*120 else '서버에 저장된 날씨입니다.'))
                target=resolve_date(p.date_ref,record['reference_at'],record['timezone'])
                if target!=resolve_date(None,record['reference_at'],record['timezone']):
                    daily=wx.get('daily') or {};days=daily.get('time') or []
                    if target not in days:raise ValueError('해당 날짜의 저장된 예보가 없습니다. 날씨 갱신 상태를 확인하세요.')
                    i=days.index(target)
                    def item(key):
                        values=daily.get(key) or [];return values[i] if i<len(values) else '미제공'
                    record['final_text']=f'{place} {target} 예보: 최저 {item("temperature_2m_min")}°C / 최고 {item("temperature_2m_max")}°C, 강수확률 {item("precipitation_probability_max")}%. 마지막 갱신 {stamp}. 저장된 예보입니다.'
            else:
                resolved=resolve_range(p.date_ref,record['reference_at'],record['timezone'])
                target_day=resolved.start
                if p.intent in {'todo.list','calendar.query'}:
                    snapshot=self.store.query_tasks(resolved.start,resolved.end,p.status,limit=50)
                    tasks=snapshot['items']
                    lines=[f'{t["date"]} {t["time"] or "시간 미지정"} · {t["title"]}'+(' [완료]' if t['completed'] else '') for t in tasks]
                    label={'all':'할 일','pending':'남은 할 일','completed':'완료한 할 일'}[p.status]
                    range_label='전체 날짜' if resolved.start=='0001-01-01' else resolved.start+(f'~{resolved.end}' if resolved.end!=resolved.start else '')
                    text=range_label+f' {label}: {snapshot["count"]}개입니다.'
                    if lines:text+='\n'+'\n'.join(lines)
                    if snapshot['truncated']:text+='\n처음 50개를 표시했습니다. 전체 목록은 할 일 위젯에서 확인하세요.'
                    record.update(state='succeeded',validation='server_read',tool_result=snapshot,final_text=text)
                    record.setdefault('routing',{}).update(source_of_truth=snapshot['source'],resolved_context={'range':snapshot['range'],'status':p.status,'timezone':record['timezone']})
                else:
                    with self.store.connect() as db:
                        receipt=db.execute('SELECT receipt_json FROM assistant_effects WHERE action_key=?',(record['action_key'],)).fetchone()
                        if receipt:
                            record.update(state='succeeded',validation='already_executed',tool_result=json.loads(receipt[0]),final_text='이 요청에서 이미 실행한 작업입니다. 다시 변경하지 않았습니다.\n'+json.loads(receipt[0])['message'])
                        else:
                            if p.intent=='todo.create':
                                if p.scope!='one':raise ValueError('한 번에 한 할 일만 등록할 수 있습니다.')
                                task=TaskCreate(title=p.title,date=target_day,time=p.time)
                                existing=db.execute('SELECT COUNT(*) FROM tasks WHERE date=? AND title=? AND time IS ?',(target_day,p.title,p.time)).fetchone()[0]
                                preview={'intent':p.intent,'task':task.model_dump(mode='json'),'targets':[], 'existing_same':existing}
                                summary=f'{target_day} {p.time or "시간 미지정"} · {p.title}\n할 일 1개를 추가합니다.'
                                if existing:summary+=f' 같은 날짜·제목·시간의 작업이 {existing}개 있습니다. 중복 추가인지 확인하세요.'
                            else:
                                rows=[dict(t) for t in db.execute('SELECT * FROM tasks WHERE date=? ORDER BY time IS NULL,time,created_at,id',(target_day,))]
                                if p.scope=='one':
                                    from .entity_resolver import resolve_target
                                    if p.date_ref is None:
                                        # A named single-turn reference can span dates; never select
                                        # 'today' merely to turn duplicate titles into one match.
                                        snapshot=self.store.query_tasks('0001-01-01','9999-12-31','all',limit=10000)
                                        candidates=snapshot['items'];truncated=snapshot['truncated']
                                    else:
                                        candidates=rows;truncated=False
                                    resolved_target=resolve_target(candidates,p.title,truncated=truncated)
                                    record['entity_resolution']=resolved_target.evidence()
                                    if resolved_target.status!='resolved':raise ValueError('일치하는 할 일이 없거나 같은 제목이 여러 개입니다. 정확한 제목과 날짜를 확인하세요.')
                                    rows=list(resolved_target.matches)
                                    target_day=rows[0]['date']
                                    if p.date_ref is None:
                                        record['resolved_date']={'start':target_day,'end':target_day,
                                            'evidence':None,'policy':'server_resolved_target_date; no_source_date'}
                                if p.scope=='all' and p.status!='all': rows=[t for t in rows if bool(t['completed'])==(p.status=='completed')]
                                if p.scope=='all' and len(rows)>100:raise ValueError('일괄 변경은 한 번에 100개까지입니다. 관리자에서 범위를 줄여 주세요.')
                                if p.intent=='todo.complete':rows=[t for t in rows if not t['completed']]
                                if p.intent=='todo.uncomplete':rows=[t for t in rows if t['completed']]
                                if not rows:
                                    record.update(state='succeeded',validation='no_change',tool_result={'count':0,'changed':False},final_text='변경할 항목이 없습니다. 이미 요청한 상태이거나 등록된 할 일이 없습니다.')
                                    self.put(rid,record);return record
                                targets=[{k:t[k] for k in ('id','version','title','date','time','completed')} for t in rows]
                                preview={'intent':p.intent,'date':target_day,'scope':p.scope,'status':p.status,'targets':targets}
                                verb={'todo.complete':'완료','todo.uncomplete':'완료 취소','todo.delete':'삭제'}[p.intent]
                                summary=f'{target_day}의 할 일 {len(rows)}개를 {verb}합니다.\n'+'\n'.join(t['title'] for t in rows[:12])
                                if len(rows)>12:summary+=f'\n외 {len(rows)-12}개'
                            preview['expires_at']=time.time()+600;preview['reference_timezone']=record['timezone']
                            record.update(state='awaiting_confirmation',validation='confirmation_required',preview=preview,preview_sha256=digest(preview),
                                          final_text=summary+'\n아직 변경하지 않았습니다. 관리자에서 내용을 확인하고 실행하세요.')
        except ValidationError as exc:
            record.update(state='needs_clarification',validation='invalid_model_proposal',
                          validation_fields=[list(e['loc']) for e in exc.errors()],
                          final_text='필요한 값이 없거나 형식이 맞지 않습니다. 날짜·제목·시간을 확인해 다시 요청해 주세요. 변경하지 않았습니다.')
        except (ValueError,TypeError) as exc:
            record.update(state='needs_clarification',validation='rejected',final_text=str(exc)[:1200])
        self.put(rid,record);return record

    def confirm(self,rid,token):
        """Atomic and idempotent. Caller must be an authenticated admin."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self.get(rid,db)
            if not record:raise HTTPException(404,'실행할 요청이 없습니다.')
            row=db.execute('SELECT status FROM llm_requests WHERE id=?',(rid,)).fetchone()
            existing=db.execute('SELECT receipt_json FROM assistant_effects WHERE action_key=?',(record['action_key'],)).fetchone()
            if existing:
                receipt=json.loads(existing[0])
                record.update(state='succeeded',validation='already_executed',tool_result=receipt,final_text=receipt['message'])
                self._finish(db,rid,record)
                return {'duplicate':True,'changed':False,'receipt':receipt}
            if row['status']!='awaiting_confirmation' or record['state']!='awaiting_confirmation':raise HTTPException(409,'확인 대기 중인 변경 요청이 아닙니다.')
            p=record.get('preview') or {}
            if token!=record.get('preview_sha256') or digest(p)!=token:raise HTTPException(409,'실행할 내용이 달라졌습니다. 다시 확인하세요.')
            if time.time()>p.get('expires_at',0):raise HTTPException(409,'확인 시간이 만료되었습니다. 새 요청으로 다시 확인하세요.')
            if record.get('protocol')==PROTOCOL and aware(utcnow(),record['timezone']).date()!=aware(record['reference_at'],record['timezone']).date():
                raise HTTPException(409,'요청 후 날짜가 바뀌었습니다. 새 요청으로 날짜와 대상을 다시 확인하세요.')
            current_settings=json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
            if current_settings['timezone']!=p['reference_timezone']:raise HTTPException(409,'시간대가 변경되었습니다. 새 요청으로 날짜를 다시 확인하세요.')
            # No model can provide raw SQL/IDs here: targets were resolved by server.
            targets=p['targets'];ids=[];intent=p['intent'];stamp=utcnow()
            for t in targets:
                now=db.execute('SELECT * FROM tasks WHERE id=?',(t['id'],)).fetchone()
                if not now or any(now[k]!=t[k] for k in ('version','title','date','time','completed')):
                    raise HTTPException(409,'확인 후 할 일이 변경되거나 삭제되었습니다. 새 요청으로 다시 확인하세요.')
            if intent=='todo.create':
                task=TaskCreate.model_validate(p['task'])
                if db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]>=10000:raise HTTPException(422,'전체 작업은 최대 10,000개입니다.')
                existing_count=db.execute('SELECT COUNT(*) FROM tasks WHERE date=? AND title=? AND time IS ?',(task.date.isoformat(),task.title,task.time)).fetchone()[0]
                if existing_count!=p['existing_same']:raise HTTPException(409,'같은 작업의 등록 상태가 바뀌었습니다. 새 요청으로 중복 여부를 확인하세요.')
                tid=uid();ids=[tid]
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,0,NULL,1,?,?)',(tid,task.title,task.date.isoformat(),task.time,task.category,task.priority,task.notes,stamp,stamp))
                message=f'{task.date.isoformat()} {task.time or "시간 미지정"} · {task.title} — 할 일 1개를 추가했습니다.'
            elif intent in {'todo.complete','todo.uncomplete','todo.delete'}:
                for t in targets:
                    ids.append(t['id'])
                    if intent=='todo.delete':db.execute('DELETE FROM tasks WHERE id=?',(t['id'],))
                    else:db.execute('UPDATE tasks SET completed=?,version=version+1,updated_at=? WHERE id=?',(int(intent=='todo.complete'),stamp,t['id']))
                verb={'todo.complete':'완료 처리','todo.uncomplete':'완료 취소','todo.delete':'삭제'}[intent]
                message=f'{p["date"]}의 할 일 {len(ids)}개를 {verb}했습니다.\n'+'\n'.join(t['title'] for t in targets[:50])
            else:raise HTTPException(422,'허용하지 않은 실행 유형입니다.')
            receipt={'intent':intent,'task_ids':ids,'count':len(ids),'message':message,'executed_at':stamp,'actor':'manager-confirmation'}
            db.execute('INSERT INTO assistant_effects VALUES(?,?,?,?)',(record['action_key'],rid,dump(receipt),stamp))
            record.update(state='succeeded',validation='executed',tool_result=receipt,final_text=message)
            self._finish(db,rid,record)
        return {'duplicate':False,'changed':True,'receipt':receipt}

    def _finish(self,db,rid,record):
        self.put(rid,record,db)
        raw=db.execute('SELECT response_json FROM llm_requests WHERE id=?',(rid,)).fetchone()[0]
        result=json.loads(raw) if raw else {}
        result.update(output=record['final_text'],output_source='server',action_state=record['state'])
        db.execute("UPDATE llm_requests SET status='succeeded',response_json=?,updated_at=?,finished_at=? WHERE id=?",(dump(result),utcnow(),utcnow(),rid))
