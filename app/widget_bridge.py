"""Assistant-to-Protocol boundary. The model proposes; it never supplies authority/results.

Reuses WidgetRequest fields, the registered input models and the existing journal,
confirmation endpoint and effect ledger. Old deterministic task writes are unchanged.
New confirmed Adapter writes reserve a durable ledger entry before dispatch; a crash
in the service/receipt gap is *uncertain*, never permission to retry a mutation.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
import json
import re
import time

from fastapi import HTTPException
from pydantic import ConfigDict, ValidationError, create_model

from .assistant import detect, digest, dump, parse_clock
from .clock_service import aware, resolve_range, resolve_source_dates
from .command_semantics import status_evidence, unsupported_personal
from .fast_reads import match_read, format_read
from .command_routing import exact_alarm, read_candidates, unsafe_source
from .store import utcnow
from .timer_commands import exact_timer, source_duration, format_timer, START as TIMER_START, STOP as TIMER_STOP
from .widget_protocol import ExecutionContext, WidgetRequest, WidgetResponse, request_digest
from .widget_protocol.core import ProtocolFault

BRIDGE_VERSION = 'widget-assistant-2'
# Projection, not a second service contract. IDs, context, permissions, version
# and idempotency fields are supplied ONLY by trusted server code.
WidgetProposal = create_model('WidgetRequestProposal',
    __config__=ConfigDict(extra='forbid', strict=True),
    **{key: (WidgetRequest.model_fields[key].annotation, ...)
       for key in ('widget', 'action', 'target', 'args')})
PROPOSAL_PROMPT = (
    'Return one WidgetRequest proposal JSON only: widget,action,target,args. '
    'Select only a listed action. Never answer, invent stored state, claim success, or execute. '
    'Copy text from the input. Unknown/missing slots are null, never "unknown". '
    'Never repair or guess numbers, dates, times or IDs. '
    'Target is null or an input-grounded reference; server resolves IDs and versions. '
    'No SQL, commands, authority or extra fields. '
    'If no listed action fits, return {"widget":null,"action":null,"target":null,"args":{}}. /no_think'
)
# Linguistic routing hints, not a duplicate capability catalog. All actions and
# argument models are looked up from WidgetRegistry for every new request.
DOMAIN_WORDS = {
    'memo': r'메모(?!리)|노트',
    'todo': r'할\s*일|해야\s*(?:할|하는)\s*일',
    'calendar': r'일정|스케줄|달력|캘린더',
    'alarm': r'알람|알림|깨워|깨우',
    'timer': r'타이머|timer',
}
ACTION_WORDS = {
    'list': r'읽|보여|알려|확인|조회|목록|어떤|뭐|남아|남았|적혀',
    'read': r'읽|보여|알려|확인|조회|내용|적혀|브리핑',
    'get': r'읽|보여|알려|확인|조회|찾아',
    'add': r'추가|등록|넣어',
    'write': r'적어|작성|기록|저장|써\s*줘|교체|바꿔|수정',
    'append': r'덧붙|이어서|추가',
    'clear': r'비워|본문.*지워|내용.*지워',
    'complete': r'완료|끝내',
    'reopen': r'완료\s*취소|미완료|다시\s*열',
    'delete': r'삭제|지워',
    'update': r'수정|고쳐|바꿔|변경',
    'set': r'맞춰|설정|울려|깨워|깨우|등록',
    'cancel': r'취소|꺼|끄|해제',
    'start': TIMER_START,
    'stop': TIMER_STOP,
}
CONTROLLED_ARGS = {'version', 'limit', 'category', 'priority', 'notes', 'repeat',
                   'weekdays', 'enabled', 'timezone', 'pinned', 'shared'}
PLACEHOLDERS = {'', 'unknown', 'none', 'null', 'n/a'}
READ_AUTHORITY = ExecutionContext(principal='assistant-reader', role='admin',
                                  permissions=frozenset({'read'}))
PREVIEW_AUTHORITY = ExecutionContext(principal='assistant-manager', role='admin',
                                     permissions=frozenset({'read', 'write', 'control'}))


def domain_for(text: str, registry) -> str | None:
    matches = [name for name, pattern in DOMAIN_WORDS.items()
               if registry.get(name) and re.search(pattern, text)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None  # Don't arbitrarily route a mixed-domain instruction.
    # Intent recovery only: no replacement of a misheard noun, time or title.
    if registry.get('alarm') and re.search(r'맞춰|깨워|울려', text):
        return 'alarm'
    if registry.get('todo') and re.search(r'추가|등록|완료|삭제', text):
        return 'todo'
    return None


def action_evidence(action: str, text: str) -> bool:
    pattern = ACTION_WORDS.get(action)
    if pattern is None or not re.search(pattern, text):
        return False
    if action == 'complete' and re.search(r'완료\s*취소|미완료', text):
        return False
    if action in {'list', 'read', 'get'} and re.search(
            r'추가|등록|삭제|지워|비워|적어|써\s*줘|저장|고쳐|수정|맞춰|설정|완료\s*(?:처리|해)', text):
        return False
    return True


def compact(schema):
    """Strip annotations, not constraints. No new independently maintained schema."""
    if isinstance(schema, list):
        return [compact(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    return {k: ({name: compact(child) for name, child in v.items()}
                if k in {'properties', '$defs', 'definitions'} else compact(v))
            for k, v in schema.items()
            if k not in {'title', 'description', 'default', 'examples'}}


def optional_schema(schema):
    if schema.get('type') == 'null' or 'null' in schema.get('type', []):
        return schema
    if any(s.get('type') == 'null' for s in schema.get('anyOf', [])):
        return schema
    return {'anyOf': [schema, {'type': 'null'}]}


def source_clocks(text: str) -> list[str | None]:
    """Use the existing civil-clock parser, never recover numbers from acoustics."""
    values = []
    pattern = (r'(?<![가-힣\d-])(?:오전|오후|아침|저녁|밤|낮)?\s*'
               r'(?:\d{1,2}|열두|열한|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:시|:)')
    for m in re.finditer(pattern, text):
        tm, rest, err = parse_clock(text[m.start():].lstrip())
        if re.match(r'(?:[영공일이삼사오육칠팔구십백몇]+|\d+)\s*(?:분|초)', rest):
            err = 'unparsed_minutes'
        values.append(None if err else tm)
    if not values and re.search(r'(?:\d+|열세|몇)\s*(?:시|:)', text):
        values.append(None)
    if values and re.search(r'아마|쯤|정도|몇\s*시|잘못|불확실', text):
        return [None]
    return values


def slot_schema(record, key: str, fallback):
    """Limit critical literals to independently proven transcript values + null."""
    if not record or key not in {'date', 'start', 'end', 'time'}:
        return fallback
    proof = None
    if key == 'time':
        clocks = source_clocks(record['normalized'])
        proof = clocks[0] if len(clocks) == 1 else None
    else:
        try:
            dates = resolve_source_dates(record['normalized'], record['reference_at'], record['timezone'])
            if dates:
                r = dates[0]
                proof = r.start if key == 'start' or (key == 'date' and r.start == r.end) else r.end if key == 'end' else None
        except ValueError:
            pass
    return {'enum': [proof, None]} if proof is not None else {'type': 'null'}


def proposal_schema(registry, domain: str, actions: list[str], record=None) -> dict:
    fields = compact(WidgetRequest.model_json_schema())
    branches = []
    for cap in registry.get_capabilities(domain):
        if cap.action not in actions:
            continue
        source = compact(cap.input_schema)
        props = {k: slot_schema(record, k, optional_schema(v)) for k, v in source.get('properties', {}).items()
                 if k not in CONTROLLED_ARGS}
        # Missing required slots remain expressible; actual Adapter validation
        # maps null/missing to clarification instead of forcing hallucinated data.
        args = {'type': 'object', 'properties': props, 'additionalProperties': False}
        if cap.target_types:
            target = {'anyOf': [fields['$defs']['WidgetTarget'], {'type': 'null'}]}
        else:
            target = {'type': 'null'}
        branches.append({'type': 'object', 'properties': {
            'widget': {'const': domain}, 'action': {'const': cap.action},
            'target': target, 'args': args},
            'required': ['widget', 'action', 'target', 'args'], 'additionalProperties': False})
    return {'anyOf': branches}


def safe_json(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode('utf-8')) > 16384:
        raise ValueError('Invalid proposal size')
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj:
                raise ValueError('Duplicate JSON key')
            obj[k] = v
        return obj
    value = json.loads(text, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
    if not isinstance(value, dict):
        raise ValueError('Expected object')
    return value


def normalize_missing(value: dict) -> tuple[dict, list[str]]:
    value = deepcopy(value)
    changes = []
    def empty(v):
        return isinstance(v, str) and v.strip().casefold() in PLACEHOLDERS
    if empty(value.get('target')):
        value['target'] = None
        changes.append('target')
    if isinstance(value.get('target'), dict) and empty(value['target'].get('value')):
        value['target']['value'] = None
        changes.append('target.value')
    if isinstance(value.get('args'), dict):
        for key in ('date', 'start', 'end', 'time'):
            if empty(value['args'].get(key)):
                value['args'][key] = None
                changes.append('args.' + key)
    return value, changes


def fail(request, status, code, message, field=None, registry=None):
    adapter = registry.get(request.widget) if registry and request else None
    from .widget_protocol import WidgetRegistry
    return WidgetRegistry.failure(request, status, code, message, adapter, field)


def fresh(record, store):
    at = utcnow()
    ref = aware(record['reference_at'], record['timezone'])
    now = aware(at, record['timezone'])
    if (store.get('settings')['timezone'] != record['timezone'] or
            now.date() != ref.date() or abs((now - ref).total_seconds()) > 900):
        raise ProtocolFault('needs_clarification', 'STALE_REQUEST',
                            '요청의 기준 날짜·시간대가 바뀌었습니다. 새 요청으로 확인해 주세요.')


class WidgetBridge:
    def __init__(self, store, engine, registry):
        self.store, self.engine, self.registry = store, engine, registry
        self.confirm_lock = asyncio.Lock()

    def has_domain(self, text: str) -> bool:
        return read_candidates(text) or domain_for(text, self.registry) is not None or any(
            re.search(pattern, text) for pattern in DOMAIN_WORDS.values())

    def prepare(self, record: dict) -> dict:
        """EXACT -> local plan; CANDIDATE -> constrained parser; UNSAFE -> clarify."""
        text = record['normalized']
        domain = domain_for(text, self.registry)
        if domain == 'timer':
            return self.prepare_timer(record)
        personal = self.has_domain(text)
        if personal:
            record['_widget_domain_request'] = True
        if re.search(DOMAIN_WORDS['memo'], text) or read_candidates(text):
            record.update(memo_read=True, protocol_private=True)
        # Preserve existing exact reads/atomic writes, not lexical safety rejects.
        if record.get('fast_read'):
            domain = {'memo.read': 'memo', 'todo.list': 'todo', 'calendar.query': 'calendar'}[
                record['fast_read']['intent']]
        else:
            if personal and record['mode'] != 'auto':
                mode = record['mode']
                record = detect(record['raw'], record['reference_at'], record['timezone'], 'auto')
                record['mode'] = mode
                record['_widget_domain_request'] = True
                if re.search(DOMAIN_WORDS['memo'], text) or read_candidates(text):
                    record.update(memo_read=True, protocol_private=True)
            if record['route'] == 'rule':
                record['routing']['confidence'] = 'EXACT'
                return record
            mixed = sum(bool(re.search(p, text)) for p in DOMAIN_WORDS.values()) > 1
            if personal and (len(text) > 1500 or unsafe_source(text) or mixed):
                record.update(route='clarify', origin='server', state='needs_clarification',
                              final_text='부정·조건·인용·반복·복합 요청은 실행하지 않습니다. 한 작업으로 다시 요청해 주세요.')
                record['routing'].update(confidence='UNSAFE', route_reason='unsafe_source')
                return record
            if record['route'] == 'clarify' and record['final_text'] != unsupported_personal(text.rstrip('.!?。？！')):
                record['routing']['confidence'] = 'UNSAFE'
                return record  # Existing date/filter/safety errors are NOT parser permissions.
            try:
                direct = exact_alarm(text, record['reference_at'], record['timezone']) if domain == 'alarm' else None
            except ValueError as exc:
                record.update(route='clarify', origin='server', state='needs_clarification', final_text=str(exc))
                record['routing'].update(route='EXISTING_RULE', confidence='EXACT', route_reason='alarm_missing_slot')
                return record
            if direct:
                record['direct_widget'] = direct
                domain = 'alarm'
        # Unknown read noun: allow only read capabilities; the model may abstain.
        recovered = domain is None and read_candidates(text)
        if domain is None and not recovered:
            if (record['mode'] == 'auto' or personal) and record['route'] in {'parser', 'chat'}:
                record.update(route='clarify', origin='server', state='needs_clarification',
                    final_text='메모·할 일·일정·알람 중 어떤 작업을 요청하나요? 일반 질문은 관리자 일반 대화 모드를 사용해 주세요.')
                record['routing'].update(route='EXISTING_RULE', route_reason='domain_not_resolved', confidence='UNRESOLVED')
            return record
        domains = [domain] if domain else [name for name in ('memo', 'todo', 'calendar', 'alarm') if self.registry.get(name)]
        caps = [(name, c) for name in domains for c in self.registry.get_capabilities(name)]
        choices = [(name, c) for name, c in caps if action_evidence(c.action, text)]
        reason = 'widget_domain:' + domain if domain else 'read_domain_uncertain'
        if recovered:
            choices = [(name, c) for name, c in caps if c.read_only and c.action in {'read', 'list'}]
            record['candidate_read_only'] = True
        elif not choices:
            # Action-language ambiguity is a parser job, NOT terminal failure.
            # Mutation authority still requires independent source action proof in ground().
            choices = caps
            reason = 'widget_action_uncertain:' + domain
            record['candidate_action_unknown'] = True
        if any(c.action == 'list' for _, c in choices) and not re.search(r'\bID\b|아이디|항목|한\s*개', text, re.I):
            choices = [(name, c) for name, c in choices if c.action != 'get']
        if record.get('fast_read'):
            choices = [(name, c) for name, c in caps if c.action == ('read' if domain == 'memo' else 'list')]
        if record.get('direct_widget'):
            choices = [(name, c) for name, c in caps if c.action == record['direct_widget']['action']]
        if not choices:
            record.update(route='clarify', origin='server', state='needs_clarification', final_text='사용 가능한 Widget 도구가 없습니다.')
            return record
        local = bool(record.get('fast_read') or record.get('direct_widget'))
        if any(name == 'memo' for name, _ in choices):
            record.update(memo_read=True, protocol_private=any(not c.read_only for _, c in choices))
        record['widget_bridge'] = BRIDGE_VERSION
        record['routing']['confidence'] = 'EXACT' if local else 'CANDIDATE'
        record['widget_trace'] = {'domain': domain or 'read_candidates', 'candidate_domains': domains,
            'available_capabilities': [name + '.' + c.action for name, c in choices],
            'schema_validation': 'not_run', 'policy_result': 'not_run', 'widget_request': None,
            'widget_response': None, 'adapter': None, 'source_of_truth': None,
            'latency_ms': {'stt_ms': None, 'router_ms': record['routing']['router_seconds'] * 1000,
                           'llm_ms': 0.0, 'adapter_ms': 0.0, 'total_ms': None}}
        record['widget_trace']['capability_fingerprint'] = digest([
            [name, c.model_dump(mode='json')] for name, c in choices])
        if record.get('direct_widget'):
            record.update(route='rule', origin='server', state='new', final_text=None, proposal=None)
            record['routing'].update(route='EXISTING_RULE', route_reason='exact_alarm.' + record['direct_widget']['action'],
                                     resolved_intent='alarm.' + record['direct_widget']['action'])
        elif not record.get('fast_read'):
            record.update(route='parser', origin='server', state='new', final_text=None, proposal=None)
            record['routing'].update(route='LLM_FALLBACK', route_reason=reason)
            branches = []
            for name in domains:
                actions = [c.action for n, c in choices if n == name]
                if actions:
                    branches.extend(proposal_schema(self.registry, name, actions, record)['anyOf'])
            branches.append({'type': 'object', 'properties': {
                'widget': {'type': 'null'}, 'action': {'type': 'null'},
                'target': {'type': 'null'}, 'args': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
                'required': ['widget', 'action', 'target', 'args'], 'additionalProperties': False})
            record['widget_trace']['proposal_schema'] = {'anyOf': branches}
        return record

    def prepare_timer(self, record):
        text = record['normalized']
        record['_widget_domain_request'] = True
        record.pop('fast_read', None)
        # Do not let broad timer fallback silently omit scheduling/batch/negation.
        if unsafe_source(text) or re.search(r'전부|모두|모든|전체|반복|마다|일시\s*정지|재개|늘려|줄여', text):
            record.update(route='clarify', origin='server', state='needs_clarification',
                          final_text='한 개의 타이머 시작·종료·조회를 요청해 주세요. 일괄·반복·일시정지는 지원하지 않습니다.')
            record['routing'].update(route='EXISTING_RULE', confidence='UNSAFE', route_reason='timer_unsafe_source')
            return record
        try:
            direct = exact_timer(text)
        except ValueError as exc:
            record.update(route='clarify', origin='server', state='needs_clarification', final_text=str(exc))
            record['routing'].update(route='EXISTING_RULE', confidence='EXACT', route_reason='timer_missing_or_invalid_duration')
            return record
        caps = self.registry.get_capabilities('timer')
        choices = [c for c in caps if c.action == direct['action']] if direct else [c for c in caps if action_evidence(c.action, text)]
        if not choices:
            choices = caps
        record['widget_bridge'] = BRIDGE_VERSION
        record['widget_trace'] = {'domain':'timer', 'candidate_domains':['timer'],
            'available_capabilities':['timer.'+c.action for c in choices],
            'schema_validation':'not_run', 'policy_result':'not_run', 'widget_request':None,
            'widget_response':None, 'adapter':None, 'source_of_truth':None,
            'capability_fingerprint':digest([['timer',c.model_dump(mode='json')] for c in choices]),
            'latency_ms':{'stt_ms':None,'router_ms':0.,'llm_ms':0.,'adapter_ms':0.,'total_ms':None}}
        record.update(origin='server', state='new', final_text=None, proposal=None)
        if direct:
            record.update(route='rule', direct_widget=direct)
            record['routing'].update(route='EXISTING_RULE', confidence='EXACT', route_reason='exact_timer.'+direct['action'], resolved_intent='timer.'+direct['action'])
        else:
            record.update(route='parser')
            record['routing'].update(route='LLM_FALLBACK',confidence='CANDIDATE',route_reason='timer_candidate')
            record['widget_trace']['proposal_schema'] = proposal_schema(self.registry,'timer',[c.action for c in choices],record)
        return record

    async def stage_timer(self, rid, record, result, *, direct=False):
        request = None
        trace = record['widget_trace']
        trace['latency_ms']['llm_ms'] = record['routing'].get('llm_seconds',0)*1000
        try:
            fresh(record,self.store)
            request, spec = self.validate_projection(record,result)
            text = record['normalized']
            # Model never chooses a duration or target not backed by the transcript.
            if request.action == 'start':
                duration = source_duration(text)
                if request.args.get('duration_seconds') != duration or not action_evidence('start',text):
                    raise ValueError('source duration or action mismatch')
                label = request.args.get('label')
                if label is not None and label != '타이머' and label not in text:
                    raise ValueError('invented timer label')
            if request.action in {'stop', 'get'}:
                if request.action == 'stop' and not action_evidence('stop',text):
                    raise ValueError('stop not requested')
                if re.search(r'전부|모두|모든|전체|다른|두\s*개|여러|후|뒤|내일|오늘|오전|오후|예약',text):
                    raise ValueError('unsupported timer target/schedule')
                if request.target is None:
                    raise ValueError('target missing')
                if request.target.type == 'item_id' and request.target.value not in text:
                    raise ValueError('invented target ID')
                if request.target.type == 'reference':
                    if request.target.value != 'current':
                        raise ValueError('unsupported timer reference')
                    exact = exact_timer(text)
                    if not (exact and (exact.get('target') or {}).get('value') == 'current') and not re.search(r'현재|지금|마지막|최근',text):
                        raise ValueError('current timer not grounded')
            if direct:
                trace['schema_validation']='server_generated'
            response = await self.registry.execute(request, ExecutionContext(
                principal='assistant-timer',role='admin',permissions=frozenset({'read','control'})))
            trace['latency_ms']['adapter_ms'] += response.meta.latency_ms
        except ProtocolFault as exc:
            response=fail(request,exc.status,exc.error.code,exc.error.message,exc.error.field,self.registry)
        except (ValueError,TypeError,ValidationError):
            response=fail(request,'needs_clarification','TIMER_SOURCE_NOT_GROUNDED',
                '1~600초의 시간 또는 종료할 현재 타이머를 명확히 말씀해 주세요. 추측해서 실행하지 않았습니다.',registry=self.registry)
        return self.finish(rid,record,request,response)

    def fingerprint(self, trace):
        return digest([[name, c.model_dump(mode='json')]
            for name in trace.get('candidate_domains', [trace['domain']])
            for c in self.registry.get_capabilities(name)
            if name + '.' + c.action in trace['available_capabilities']])

    def payload(self, record, cfg):
        trace = record['widget_trace']
        schema = trace['proposal_schema']
        # Only this request's candidate domain/action argument schemas enter the prompt.
        contract = [{b['properties']['widget']['const'] + '.' + b['properties']['action']['const']: b['properties']['args']}
                    for b in schema['anyOf'] if 'const' in b['properties']['action']]
        ctx = record['time_context']
        prompt = (PROPOSAL_PROMPT + '\nwidgets=' + ','.join(trace['candidate_domains']) + '; args=' + dump(contract) +
                  '\n오늘=' + ctx['today'] + '; 내일=' + ctx['tomorrow'] + '; timezone=' + record['timezone'])
        return dump({'model': cfg.model,
            'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': record['normalized']}],
            'stream': False, 'max_tokens': min(cfg.max_tokens, 64), 'temperature': 0,
            'chat_template_kwargs': {'enable_thinking': False},
            # Verified in pinned b6000 tools/server/utils.hpp, not just a JSON-only prompt.
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'widget_request_proposal', 'strict': True, 'schema': schema}}})

    async def query(self, request, record):
        response = await self.registry.execute(request, READ_AUTHORITY)
        record['widget_trace']['latency_ms']['adapter_ms'] += response.meta.latency_ms
        return response

    def finish(self, rid, record, request, response, *, fast_plan=None):
        trace = record['widget_trace']
        trace.update(widget_request=request.model_dump(mode='json') if request else None,
                     widget_response=response.model_dump(mode='json'),
                     adapter=response.meta.adapter, source_of_truth=response.meta.source_of_truth,
                     policy_result=response.status)
        record['routing'].update(source_of_truth=response.meta.source_of_truth,
                                 resolved_intent=(request.widget + '.' + request.action) if request else None)
        record['origin'] = 'server'
        spec = self.registry.get(request.widget).operations.get(request.action) if request and self.registry.get(request.widget) else None
        record['validation'] = ('server_read' if spec and spec.read_only else 'executed') if response.status == 'success' else response.status
        record['tool_result'] = response.data or {}
        record['state'] = {'success': 'succeeded', 'needs_confirmation': 'awaiting_confirmation',
                          'error': 'failed', 'unavailable': 'needs_clarification'}.get(response.status, 'needs_clarification')
        if response.status == 'success':
            data = response.data
            if request.widget == 'timer':
                record['final_text'] = format_timer(request, data, response.meta.duplicate)
                record['routing']['resolved_context'] = {'selection_policy': 'latest_started_running' if request.target and request.target.value == 'current' else 'explicit', 'timer_id': data.get('id'), 'current_id': data.get('current_id')}
            elif request.widget == 'memo' and request.action == 'read':
                # Preserve the existing dynamic display privacy recheck contract.
                snapshot = data | {'source': response.meta.source_of_truth, 'count': 1,
                                  'note_id': data.get('id'), 'as_of': response.meta.executed_at}
                record['memo_read'] = True
                record['tool_result'] = snapshot
                label = '가장 최근에 수정된 저장 메모' if data['selection_policy'] == 'updated_at' else '기본 메모 위젯의 기본 카드' if data['selection_policy'] == 'default_widget_card' else '선택한 저장 메모'
                record['final_text'] = label + '에는 다음과 같이 적혀 있습니다:\n' + (data['body'] or '(본문이 비어 있습니다.)')
                record['routing']['resolved_context'] = {k: snapshot.get(k) for k in (
                    'selection_policy', 'widget_id', 'note_id', 'version', 'updated_at', 'shared', 'active_card_known')}
            elif request.widget in {'todo', 'calendar'} and request.action == 'list':
                if fast_plan is None:
                    from .fast_reads import ReadPlan
                    args = request.args
                    dates = resolve_source_dates(record['normalized'], record['reference_at'], record['timezone'])
                    ref = dates[0].evidence if dates else None
                    fast_plan = ReadPlan('todo.list' if request.widget == 'todo' else 'calendar.query',
                                        ref, args.get('status', 'all'), args.get('period'))
                _, record['final_text'], context = format_read(data, fast_plan, record['reference_at'], record['timezone'])
                record['routing']['resolved_context'] = context
            elif request.widget in {'todo', 'calendar'} and request.action == 'get':
                record['final_text'] = f'{data["date"]} {data["time"] or "시간 미지정"} · {data["title"]}' + (' [완료]' if data['completed'] else '')
            elif request.widget == 'alarm' and request.action == 'list':
                items = data['items']
                record['final_text'] = f'저장된 웹 알람은 {len(items)}개입니다.'
                if items:
                    record['final_text'] += '\n' + '\n'.join(f'{a["time"]} · {a["label"]}' for a in items)
                record['final_text'] += '\n활성 웹페이지용 예약이며 실제 소리 재생 여부는 확인하지 않았습니다.'
            elif request.widget == 'alarm' and request.action in {'set', 'cancel'}:
                verb = '예약했습니다' if request.action == 'set' else '예약을 취소했습니다'
                record['final_text'] = f'알람 {verb}. ID: {data["id"]}\n활성 브라우저에서 소리 허용이 필요합니다. 실제 소리 재생을 확인한 것은 아닙니다.'
            else:
                # Only reachable after a confirmed Adapter success. Never model text.
                noun = {'memo':'메모', 'todo':'할 일', 'calendar':'일정'}.get(request.widget, request.widget)
                if request.action == 'add':
                    record['final_text'] = f'{noun} {data["count"]}개를 등록했습니다.'
                elif request.action == 'delete':
                    record['final_text'] = f'{noun} {data["deleted"]}개를 삭제했습니다.'
                else:
                    verb = {'write':'저장했습니다', 'append':'본문에 내용을 덧붙였습니다',
                            'clear':'본문을 비웠습니다', 'complete':'완료 처리했습니다',
                            'reopen':'완료를 취소했습니다', 'update':'수정했습니다'}.get(request.action,'변경했습니다')
                    record['final_text'] = f'{noun}: {verb}. 실제 서비스 응답을 확인했습니다.'
        else:
            code = response.error.code if response.error else ''
            if request and request.widget == 'alarm' and response.status == 'unavailable':
                record['final_text'] = '알람 요청으로 이해했지만 알람 설정·취소 Adapter는 아직 연결되지 않았습니다. 이 요청으로 알람을 설정하지 않았습니다.'
            elif request and request.widget == 'alarm' and response.status == 'needs_clarification' and response.error.field == 'args.time':
                record['final_text'] = '알람 요청으로 이해했습니다. 오전·오후와 시간을 다시 말씀해 주세요. 시간을 추측하지 않았습니다.'
            elif response.status == 'needs_confirmation':
                record['final_text'] = '아직 변경하지 않았습니다. 관리자에서 제안한 대상과 내용을 확인해 주세요.'
            else:
                record['final_text'] = response.error.message if response.error else '요청을 완료하지 못했습니다.'
            # Empty current memo remains a normal empty read (no invented content).
            if fast_plan and fast_plan.intent == 'memo.read' and code == 'NOT_FOUND' and response.error.message == '읽을 수 있는 메모가 없습니다.':
                record.update(state='succeeded', validation='server_read', tool_result={'count': 0, 'shared': False,
                              'source': response.meta.source_of_truth}, final_text='읽을 수 있는 저장 메모가 없습니다.')
            if request and request.widget == 'memo':
                record['memo_read'] = True  # No failed/private memo details on shared displays.
        if fast_plan:
            from .fast_reads import INTENT_NAMES
            record['routing']['resolved_intent'] = INTENT_NAMES[fast_plan.intent]
        self.engine.put(rid, record)
        return record

    async def fast_read(self, rid, record):
        plan = match_read(record['normalized'], record['reference_at'], record['timezone'])
        if not plan or plan.data() != record['fast_read'] or plan.proposal() != record['proposal']:
            raise ValueError('Fast plan integrity')
        if plan.intent == 'memo.read':
            request = WidgetRequest(request_id=rid, widget='memo', action='read',
                target={'type': 'reference', 'value': 'last' if plan.memo_ref == 'last_modified' else 'current'})
        else:
            r = resolve_range(plan.date_ref, record['reference_at'], record['timezone'])
            request = WidgetRequest(request_id=rid, widget='todo' if plan.intent == 'todo.list' else 'calendar',
                action='list', args={'start': r.start, 'end': r.end, 'status': plan.status, 'period': plan.period, 'limit': 50})
            record['resolved_date'] = r.data()
        from .capabilities import require
        record['capability'] = require(plan.intent).public()
        record['widget_trace']['schema_validation'] = 'server_generated'
        try:
            fresh(record, self.store)
            response = await self.query(request, record)
        except ProtocolFault as exc:
            response = fail(request, exc.status, exc.error.code, exc.error.message, exc.error.field, self.registry)
        return self.finish(rid, record, request, response, fast_plan=plan)

    def validate_projection(self, record, result):
        trace = record['widget_trace']
        if result.get('finish_reason') != 'stop' or result.get('tool_calls') or result.get('refusal'):
            raise ProtocolFault('invalid_request', 'INCOMPLETE_PROPOSAL', '완전한 Widget 제안을 받지 못했습니다. 다시 요청해 주세요.')
        try:
            parsed = safe_json(result.get('output'))
            trace['parsed_proposal'] = parsed
            if parsed == {'widget': None, 'action': None, 'target': None, 'args': {}}:
                raise ProtocolFault('needs_clarification', 'INTENT_UNRESOLVED',
                                    '어떤 Widget 동작인지 확정하지 못했습니다. 다시 말씀해 주세요.')
            parsed, changes = normalize_missing(parsed)
            trace['missing_normalized'] = changes
            p = WidgetProposal.model_validate(parsed)
            request = WidgetRequest(request_id=record['action_key'], idempotency_key=record['action_key'],
                widget=p.widget, action=p.action, target=p.target, args=p.args,
                context={'source': 'assistant'})
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise ProtocolFault('invalid_request', 'INVALID_PROPOSAL',
                                'WidgetRequest 형식의 제안이 아닙니다. 모델 문장을 실행·조회 결과로 사용하지 않았습니다.') from None
        if request.widget + '.' + request.action not in trace['available_capabilities']:
            raise ProtocolFault('invalid_request', 'UNSUPPORTED_ACTION', '이번 요청에 허용하지 않은 capability입니다.', 'action')
        adapter = self.registry.get(request.widget)
        spec = adapter.operations.get(request.action) if adapter else None
        if spec is None:
            raise ProtocolFault('unavailable', 'CAPABILITY_UNAVAILABLE', '요청한 Adapter가 현재 연결되어 있지 않습니다.')
        expected = self.fingerprint(trace)
        if expected != trace['capability_fingerprint']:
            raise ProtocolFault('conflict', 'CAPABILITY_CHANGED', '요청 후 capability가 바뀌었습니다. 새 요청으로 확인하세요.')
        allowed = set(spec.inputs.model_fields) - CONTROLLED_ARGS
        if set(request.args) - allowed:
            raise ProtocolFault('invalid_request', 'UNTRUSTED_ARGUMENT', '모델은 서버 소유 필드나 미지원 인자를 지정할 수 없습니다.', 'args')
        if request.widget == 'memo':
            record['protocol_private'] = request.action != 'read'
        trace['schema_validation'] = 'valid'
        return request, spec

    async def ground(self, record, request, spec):
        """Strict critical-slot proof. Intent recovery never repairs transcript data."""
        text, args = record['normalized'], deepcopy(request.args)
        def reject(message, field=None):
            raise ProtocolFault('needs_clarification', 'SOURCE_NOT_GROUNDED', message, field)
        inferred_read = spec.read_only and (record.get('candidate_read_only') or record.get('candidate_action_unknown'))
        if unsafe_source(text) or (not inferred_read and not action_evidence(request.action, text)):
            reject('원문에서 단일 동작을 확인하지 못했습니다. 한 작업으로 다시 요청해 주세요.')
        if re.search(r'공유|고정|우선순위|카테고리|반복|매일|매주|매월|마다', text):
            reject('공유·고정·반복 등의 조건은 관리자 화면에서 확인해 주세요. 조건을 생략해 실행하지 않습니다.')
        if (not spec.read_only or request.widget == 'memo') and re.search(r'전체|전부|모두|모든|다\s*(?:삭제|완료|지워|비워)', text):
            reject('이 Adapter 연결은 단일 항목만 지원합니다. 여러 항목으로 추측하거나 범위를 줄여 실행하지 않습니다.')
        if spec.read_only and re.search(r'제외|빼고|말고|그중|중에서|관련|만\s*(?:보여|알려|확인)', text):
            reject('추가 필터를 안전하게 적용할 수 없습니다. 관리자 목록에서 조건을 확인해 주세요.')
        for key in ('title', 'body', 'text', 'label'):
            val = args.get(key)
            if val is not None and (not isinstance(val, str) or val not in record['raw']):
                reject('제목·본문이 전사 원문과 다릅니다. 실제 내용을 다시 말씀해 주세요.', 'args.' + key)
        if request.widget in {'todo', 'calendar', 'alarm'}:
            refs = resolve_source_dates(text, record['reference_at'], record['timezone'])
            source = refs[0] if refs else None
            default = resolve_range(None, record['reference_at'], record['timezone'])
            expected = source or default
            record['resolved_date'] = expected.data()
            if any(k in args and args[k] is not None for k in ('date', 'start', 'end')):
                if source is None:
                    reject('원문에 없는 날짜를 제안했습니다. 날짜를 다시 확인해 주세요.', 'args.date')
                expected_values = {'date': source.start if source.start == source.end else None,
                                   'start': source.start, 'end': source.end}
                for key in ('date', 'start', 'end'):
                    if args.get(key) is not None and args[key] != expected_values[key]:
                        reject('분석한 날짜가 원문과 다릅니다. 날짜를 다시 확인해 주세요.', 'args.' + key)
            if request.action == 'list' and request.widget in {'todo', 'calendar'}:
                # Read dates are server-owned. The model may omit but may not alter them.
                args.pop('date', None); args.update(start=expected.start, end=expected.end)
                state = status_evidence(text) or 'all'
                if args.get('status', state) not in {None, state}:
                    reject('완료 조건이 원문과 다릅니다.', 'args.status')
                args['status'] = state
                period = 'morning' if '오전' in text else 'afternoon' if '오후' in text else None
                if '오전' in text and '오후' in text:
                    reject('오전·오후 중 조회 범위를 하나로 말씀해 주세요.', 'args.period')
                if args.get('period', period) not in {None, period}:
                    reject('시간대 필터가 원문과 다릅니다.', 'args.period')
                if re.search(r'\d|(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*시|이전|이후|부터|까지',
                             re.sub(r'\d{4}-\d{2}-\d{2}|\d{4}년|\d{1,2}월\s*\d{1,2}일?', '', text)):
                    reject('정확한 시각·개수·구간 조건은 현재 조회에서 지원하지 않습니다. 날짜와 오전·오후로 다시 요청하세요.', 'args')
                args['period'] = period; args['limit'] = 50
            elif request.action in {'add', 'set'}:
                if args.get('date') is None:
                    reject('변경할 날짜를 명시해 주세요. 모델이 누락한 날짜를 채워 실행하지 않습니다.', 'args.date')
            if 'time' in spec.inputs.model_fields:
                clocks = source_clocks(text)
                if args.get('time') is not None and (len(clocks) != 1 or args['time'] != clocks[0]):
                    reject('분석한 시간이 전사 원문과 다릅니다. 오전·오후와 시간을 다시 말씀해 주세요.', 'args.time')
                if clocks and args.get('time') is None:
                    reject('원문 시간이 누락되었거나 불확실합니다. 시간을 다시 말씀해 주세요.', 'args.time')
        if request.widget == 'alarm' and request.action == 'set':
            if args.get('label') is None:
                args['label'] = '알람'
            args['timezone'] = record['timezone']
        if not spec.available:
            # Intent may be recovered, but an unconnected control remains unavailable.
            # Do not resolve fabricated Alarm IDs through nonexistent get/execute paths.
            raise ProtocolFault('unavailable', 'CAPABILITY_UNAVAILABLE', '이 capability는 아직 연결하지 않았습니다.')
        target = request.target
        if target and target.type in {'item_id', 'widget_id'}:
            if not re.search(r'(?<![A-Za-z0-9_-])' + re.escape(target.value or '') + r'(?![A-Za-z0-9_-])', text):
                reject('모델이 제안한 ID를 원문에서 확인할 수 없습니다.', 'target')
        if request.widget == 'memo':
            latest = bool(re.search(r'방금|아까|최근|마지막', text))
            wanted = 'last' if latest else 'current'
            if target and target.type == 'reference':
                value = 'last' if target.value == 'last_modified' else target.value
                if value not in {wanted, 'active', 'selected'}:
                    reject('메모 선택 기준이 원문과 다릅니다.', 'target')
            elif target is None and request.action == 'read':
                target = WidgetRequest(request_id='selection', widget='memo', action='read',
                                       target={'type': 'reference', 'value': wanted}).target
            if not spec.read_only:
                if request.action == 'write' and target is None:
                    # New note requires its own explicit title/body; no implicit overwrite.
                    if re.search(r'수정|교체|바꿔|현재|방금|아까', text):
                        reject('수정할 메모를 명확히 지정해 주세요. 새 메모로 대신 만들지 않습니다.', 'target')
                else:
                    lookup = WidgetRequest(request_id='resolve:' + record['action_key'], widget='memo', action='read',
                        target=target or {'type': 'reference', 'value': wanted})
                    response = await self.query(lookup, record)
                    if response.status == 'error':
                        raise ProtocolFault('error', response.error.code, response.error.message, response.error.field)
                    if response.status != 'success' or not response.data.get('id'):
                        reject('변경할 저장 메모 ID를 결정하지 못했습니다. 관리자 메모 목록에서 확인하세요.', 'target')
                    target = {'type': 'item_id', 'value': response.data['id']}
                    args['version'] = response.data['version']
                    record['widget_trace']['resolved_target'] = {k: response.data[k] for k in ('id', 'title', 'version')}
        elif request.widget == 'alarm' and request.action == 'cancel':
            if target is None or target.type != 'item_id':
                reject('취소할 알람 ID를 명확히 말씀해 주세요.', 'target')
            response = await self.query(WidgetRequest(request_id='resolve:' + record['action_key'],
                widget='alarm', action='list'), record)
            if response.status != 'success':
                raise ProtocolFault('error', 'TARGET_READ_FAILED', '실제 알람을 조회하지 못했습니다.')
            alarm = next((a for a in response.data['items'] if a['id'] == target.value), None)
            if alarm is None:
                raise ProtocolFault('not_found', 'NOT_FOUND', '해당 알람이 없습니다.', 'target')
            args['version'] = alarm['version']
            record['widget_trace']['resolved_target'] = {k: alarm[k] for k in ('id', 'label', 'version', 'date', 'time')}
        elif not spec.read_only and spec.target_required:
            if target is None:
                # Resolve an exact title mentioned in the transcript, never a fuzzy ID.
                refs = resolve_source_dates(text, record['reference_at'], record['timezone'])
                if not refs or refs[0].start != refs[0].end:
                    reject('대상의 날짜와 정확한 제목 또는 ID를 말씀해 주세요.', 'target')
                query = WidgetRequest(request_id='resolve:' + record['action_key'], widget=request.widget,
                    action='list', args={'date': refs[0].start, 'limit': 10000})
                response = await self.query(query, record)
                if response.status != 'success':
                    raise ProtocolFault('error', 'TARGET_READ_FAILED', '실제 대상을 조회하지 못했습니다. 실행하지 않았습니다.')
                matches = [t for t in response.data['items'] if t['title'] in text]
                if len(matches) != 1:
                    reject('일치하는 대상이 없거나 여러 개입니다. 정확한 대상 ID를 확인해 주세요.', 'target')
                target = {'type': 'item_id', 'value': matches[0]['id']}
                args['version'] = matches[0]['version']
                record['widget_trace']['resolved_target'] = {k: matches[0][k] for k in ('id', 'title', 'version', 'date', 'time')}
            else:
                response = await self.query(WidgetRequest(request_id='resolve:' + record['action_key'],
                    widget=request.widget, action='get', target=target), record)
                if response.status != 'success':
                    reject('실제 대상 ID를 확인하지 못했습니다.', 'target')
                args['version'] = response.data['version']
                record['widget_trace']['resolved_target'] = {k: response.data[k] for k in ('id', 'title', 'version', 'date', 'time')}
        return WidgetRequest(request_id=record['action_key'], idempotency_key=record['action_key'],
            widget=request.widget, action=request.action, target=target, args=args, context={'source': 'assistant'})

    async def direct(self, rid, record):
        if record.get('widget_trace', {}).get('domain') == 'timer':
            proof = exact_timer(record['normalized'])
            if not proof or proof != record.get('direct_widget'):
                raise ValueError('Timer direct plan integrity')
            return await self.stage_timer(rid, record, {'output': dump(proof), 'finish_reason': 'stop'}, direct=True)
        proof = exact_alarm(record['normalized'], record['reference_at'], record['timezone'])
        if not proof or proof != record.get('direct_widget'):
            raise ValueError('Direct rule plan integrity')
        result = {'output': dump(proof), 'finish_reason': 'stop'}
        record = await self.stage(rid, record, result)
        record['widget_trace']['schema_validation'] = 'server_generated'
        return record

    async def stage(self, rid, record, result):
        if record.get('widget_trace', {}).get('domain') == 'timer':
            return await self.stage_timer(rid, record, result)
        request = None
        trace = record['widget_trace']
        trace['latency_ms']['llm_ms'] = record['routing'].get('llm_seconds', 0) * 1000
        try:
            fresh(record, self.store)
            request, spec = self.validate_projection(record, result)
            # Missing alarm time takes precedence; intent can recover without numbers.
            if request.widget == 'alarm' and request.action == 'set' and request.args.get('time') is None:
                raise ProtocolFault('needs_clarification', 'MISSING_REQUIRED_ARGUMENT',
                                    '시간을 다시 말씀해 주세요.', 'args.time')
            request = await self.ground(record, request, spec)
            if spec.read_only:
                response = await self.query(request, record)
            else:
                # Validates metadata/slots/permission but has NO confirmation authority.
                response = await self.registry.execute(request, PREVIEW_AUTHORITY)
                trace['latency_ms']['adapter_ms'] += response.meta.latency_ms
            if response.status == 'invalid_request':
                trace['schema_validation'] = 'arguments_rejected'
        except ProtocolFault as exc:
            response = fail(request, exc.status, exc.error.code, exc.error.message, exc.error.field, self.registry)
            if trace['schema_validation'] == 'not_run':
                trace['schema_validation'] = 'rejected'
            trace['validation_error'] = exc.error.code
        except (ValueError, TypeError, ValidationError):
            response = fail(request, 'needs_clarification', 'INVALID_ARGUMENT',
                            '날짜·시간·대상 인자를 확인해 주세요. 실행하지 않았습니다.', registry=self.registry)
            trace['schema_validation'] = 'invalid_argument'
        record = self.finish(rid, record, request, response)
        if response.status == 'needs_confirmation':
            with closing(self.store.connect()) as db:
                old = db.execute('SELECT receipt_json FROM assistant_effects WHERE action_key=?',
                                 (record['action_key'],)).fetchone()
            if old:
                return self.replay(rid, record, json.loads(old[0]))
            preview = {'widget_request': request.model_dump(mode='json'),
                       'request_digest': request_digest(request), 'reference_timezone': record['timezone'],
                       'expires_at': time.time() + 600, 'targets': [],
                       'resolved_target': trace.get('resolved_target')}
            record.update(preview=preview, preview_sha256=digest(preview), validation='confirmation_required',
                          final_text=f'{request.widget}.{request.action}\n{dump(request.args)}\n아직 변경하지 않았습니다. 날짜·시간·대상을 확인하고 실행하세요.')
            if request.widget == 'memo':
                record['protocol_private'] = True
            self.engine.put(rid, record)
        return record

    def replay(self, rid, record, receipt):
        if receipt.get('protocol_state') in {'reserved', 'uncertain'}:
            record.update(state='needs_clarification', validation='outcome_uncertain',
                final_text='이 요청의 이전 실행 결과가 불확정합니다. 자동 재실행하지 않습니다. 실제 위젯 상태를 확인하세요.')
        else:
            record.update(state='succeeded', validation='already_executed', tool_result=receipt,
                          final_text='이 요청은 이미 처리했습니다. 다시 변경하지 않았습니다.\n' + receipt['message'])
        self.engine.put(rid, record)
        return record

    def assert_not_executing(self, rid):
        record = self.engine.get(rid)
        uncertain = False
        if record and record.get('widget_bridge'):
            with closing(self.store.connect()) as db:
                effect = db.execute('SELECT receipt_json FROM assistant_effects WHERE action_key=?',
                                    (record['action_key'],)).fetchone()
            uncertain = bool(effect and json.loads(effect[0]).get('protocol_state') in {'reserved', 'uncertain'})
        if record and (record.get('protocol_confirming') or uncertain):
            raise HTTPException(409, '확인된 Adapter 실행이 시작됐거나 결과가 불확정합니다. 실제 상태를 확인해 주세요. 변경을 취소했다고 표시하지 않습니다.')

    async def confirm(self, rid, token):
        async with self.confirm_lock:
            with closing(self.store.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                record = self.engine.get(rid, db)
                if not record or record.get('widget_bridge') != BRIDGE_VERSION:
                    raise HTTPException(409, '현재 Widget 제안으로 다시 확인해 주세요.')
                old = db.execute('SELECT receipt_json FROM assistant_effects WHERE action_key=?', (record['action_key'],)).fetchone()
                if old:
                    receipt = json.loads(old[0])
                    if receipt.get('protocol_state') in {'reserved', 'uncertain'}:
                        raise HTTPException(409, '이전 실행 결과가 불확정합니다. 실제 상태를 확인하고 새 요청으로 진행하세요.')
                    return {'duplicate': True, 'changed': False, 'receipt': receipt}
                status = db.execute('SELECT status FROM llm_requests WHERE id=?', (rid,)).fetchone()[0]
                p = record.get('preview') or {}
                if status != 'awaiting_confirmation' or record['state'] != 'awaiting_confirmation':
                    raise HTTPException(409, '확인 대기 요청이 아닙니다.')
                if token != record.get('preview_sha256') or token != digest(p):
                    raise HTTPException(409, '확인할 내용이 달라졌습니다.')
                if time.time() > p.get('expires_at', 0):
                    raise HTTPException(409, '확인 시간이 만료됐습니다.')
                try:
                    fresh(record, self.store)
                    request = WidgetRequest.model_validate(p['widget_request'])
                except (ValueError, ProtocolFault):
                    raise HTTPException(409, '날짜·시간대·제안이 달라졌습니다. 새 요청으로 확인하세요.') from None
                if request_digest(request) != p.get('request_digest'):
                    raise HTTPException(409, '실행 제안 무결성을 확인하지 못했습니다.')
                if self.fingerprint(record['widget_trace']) != record['widget_trace']['capability_fingerprint']:
                    raise HTTPException(409, '확인 후 capability가 달라졌습니다. 새 요청으로 진행하세요.')
                # Durable reservation BEFORE entering any Adapter. With no new DB
                # transaction plumbing, a crash gap is uncertain (not exactly-once).
                receipt = {'protocol_state': 'reserved', 'request_digest': request_digest(request),
                           'message': '실행 결과 확인 필요', 'executed_at': None}
                db.execute('INSERT INTO assistant_effects VALUES(?,?,?,?)',
                    (record['action_key'], rid, dump(receipt), utcnow()))
                record['protocol_confirming'] = True
                self.engine.put(rid, record, db)
                db.execute("UPDATE llm_requests SET status='running' WHERE id=?", (rid,))
            authority = ExecutionContext(principal='assistant-manager', role='admin',
                permissions=frozenset({'read', 'write', 'control'}), confirmed_digest=request_digest(request))
            try:
                response = await self.registry.execute(request, authority)
            except BaseException:
                # Cancellation may race a to_thread commit: persist uncertainty,
                # never tell the user that cancellation undid a service mutation.
                with closing(self.store.connect()) as db, db:
                    receipt['protocol_state'] = 'uncertain'
                    db.execute('UPDATE assistant_effects SET receipt_json=? WHERE action_key=?',
                               (dump(receipt), record['action_key']))
                    record.update(protocol_confirming=False, state='needs_clarification',
                        validation='outcome_uncertain', final_text='실행 결과를 확정할 수 없습니다. 자동 재실행하지 말고 실제 Widget 상태를 확인하세요.')
                    self.engine.put(rid, record, db)
                    previous = db.execute('SELECT response_json FROM llm_requests WHERE id=?',(rid,)).fetchone()
                    result = (json.loads(previous[0]) if previous and previous[0] else {}) | {
                        'output':record['final_text'], 'output_source':'server', 'action_state':record['state']}
                    db.execute('UPDATE llm_requests SET status=?,response_json=?,updated_at=? WHERE id=?',
                               (record['state'], dump(result), utcnow(), rid))
                raise
            record = self.finish(rid, record, request, response)
            record['protocol_confirming'] = False
            receipt = {'protocol_state': 'complete' if response.status == 'success' else 'uncertain',
                       'request_digest': request_digest(request), 'widget_response': response.model_dump(mode='json'),
                       'message': record['final_text'], 'executed_at': response.meta.executed_at}
            with closing(self.store.connect()) as db, db:
                db.execute('UPDATE assistant_effects SET receipt_json=? WHERE action_key=?',
                           (dump(receipt), record['action_key']))
                self.engine.put(rid, record, db)
                value = {'output': record['final_text'], 'output_source': 'server', 'action_state': record['state']}
                prev = db.execute('SELECT response_json FROM llm_requests WHERE id=?', (rid,)).fetchone()
                value = (json.loads(prev[0]) if prev and prev[0] else {}) | value
                db.execute('UPDATE llm_requests SET status=?,response_json=?,updated_at=?,finished_at=? WHERE id=?',
                           (record['state'], dump(value), utcnow(), utcnow(), rid))
            return {'duplicate': False, 'changed': response.meta.changed, 'receipt': receipt}
