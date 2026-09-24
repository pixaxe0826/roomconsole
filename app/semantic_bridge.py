"""M3 coordinator on the existing WidgetBridge validation/confirmation/Adapter path.

Pure source parsing is separated from read-only target lookup. This module never
supplies confirmed_digest and never calls an Adapter's mutation service directly.
"""
from __future__ import annotations

import re
import time

from .entity_resolver import resolve_target
from .semantic_parser import parse_semantic
from .semantic_types import SEMANTIC_VERSION
from .widget_protocol import WidgetRequest
from .widget_protocol.core import ProtocolFault


def prepare_semantic(bridge, record):
    if record.get('fast_read'):
        return None
    text, at, tz = record['normalized'], record['reference_at'], record['timezone']
    # Keep the established exact alarm path, including its specific missing-slot messages.
    if re.search(r'알람|알림', text):
        from .command_routing import exact_alarm
        try:
            if exact_alarm(text, at, tz):
                return None
        except ValueError:
            if not re.search(r'분\s*(?:뒤|후)|시간\s*(?:뒤|후)', text):
                return None
    started = time.perf_counter()
    frame = parse_semantic(text, at, tz, raw=record['raw'])
    elapsed = (time.perf_counter() - started) * 1000
    if frame is None:
        return None
    previous = record.get('proposal') or {}
    # Established exact atomic legacy writes keep their preview and durable receipt
    # contract. Do not route them to a different engine just to match a gold alias.
    if record.get('route') == 'rule' and previous.get('scope') == 'one' and frame.confidence == 'EXACT':
        expected = {'add': 'todo.create', 'complete': 'todo.complete', 'reopen': 'todo.uncomplete', 'delete': 'todo.delete'}
        title = dict(frame.arguments).get('title') if frame.action == 'add' else frame.target_text
        if (frame.widget == 'todo' and previous.get('intent') == expected.get(frame.action)
                and previous.get('title') == title):
            return None
    adapter = bridge.registry.get(frame.widget)
    spec = adapter.operations.get(frame.action) if adapter else None
    if spec is None:
        return None  # The parser never registers a new operation.
    from .assistant import digest
    from .widget_bridge import BRIDGE_VERSION
    cap = next(c for c in bridge.registry.get_capabilities(frame.widget) if c.action == frame.action)
    record.pop('fast_read', None)
    record.update(route='rule', origin='server', state='new', final_text=None, proposal=None,
                  _widget_domain_request=True, widget_bridge=BRIDGE_VERSION,
                  semantic_frame=frame.data(), direct_widget=frame.proposal(),
                  semantic_parser={'enabled': True, 'version': SEMANTIC_VERSION, 'parser_ms': elapsed,
                                   'scope': 'single_turn_source_spans; no authority or entity IDs'})
    record['routing'].update(route='SEMANTIC_PARSER', route_reason='semantic.' + frame.widget + '.' + frame.action,
                             resolved_intent=frame.widget + '.' + frame.action, confidence=frame.confidence)
    record['widget_trace'] = {
        'domain': frame.widget, 'candidate_domains': [frame.widget],
        'available_capabilities': [frame.widget + '.' + frame.action],
        'capability_fingerprint': digest([[frame.widget, cap.model_dump(mode='json')]]),
        'schema_validation': 'not_run', 'policy_result': 'not_run', 'widget_request': None,
        'widget_response': None, 'adapter': None, 'source_of_truth': None,
        'semantic_parser': record['semantic_parser'], 'source_evidence': frame.data(),
        'latency_ms': {'stt_ms': None, 'router_ms': 0., 'llm_ms': 0., 'adapter_ms': 0., 'total_ms': None}}
    return record


def verified_frame(record):
    from .assistant import normalize
    if normalize(record['raw'])[0] != record['normalized']:
        raise ProtocolFault('needs_clarification', 'SEMANTIC_SOURCE_CHANGED', '저장된 원문과 분석 기준이 달라졌습니다. 새 요청으로 확인하세요.')
    frame = parse_semantic(record['normalized'], record['reference_at'], record['timezone'], raw=record['raw'])
    if frame is None or frame.data() != record.get('semantic_frame') or frame.proposal() != record.get('direct_widget'):
        raise ProtocolFault('needs_clarification', 'SEMANTIC_PLAN_CHANGED',
                            '저장된 분석 계획이 원문과 다릅니다. 새 요청으로 확인하세요.')
    return frame


async def ground_semantic(bridge, record, request, spec):
    frame = verified_frame(record)
    if request.widget != frame.widget or request.action != frame.action or request.args != dict(frame.arguments) or request.target is not None:
        raise ProtocolFault('needs_clarification', 'SEMANTIC_PLAN_CHANGED', '원문에서 확인한 제안만 사용할 수 있습니다.')
    if frame.confidence != 'EXACT':
        raise ProtocolFault('needs_clarification', frame.issue or 'SEMANTIC_NOT_EXACT',
                            frame.message or '요청을 명확히 다시 말씀해 주세요.', frame.field)
    if not spec.available:
        raise ProtocolFault('unavailable', 'CAPABILITY_UNAVAILABLE', '현재 사용할 수 없는 operation입니다.')
    args = dict(frame.arguments)
    fact = frame.temporal
    if fact.start:
        record['resolved_date'] = {'start': fact.start, 'end': fact.end,
                                   'evidence': fact.date_ref, 'policy': 'semantic_source_spans'}
    target = None
    if request.action == 'list':
        args['limit'] = 50  # Only the server chooses the output cap.
    elif request.action == 'add':
        response = await bridge.query(WidgetRequest(
            request_id='duplicate:' + record['action_key'], widget=request.widget, action='list',
            args={'start': fact.start, 'end': fact.end, 'status': 'all', 'limit': 10000}), record)
        if response.status != 'success':
            raise ProtocolFault('error', 'TARGET_READ_FAILED', '기존 항목 확인에 실패했습니다. 변경하지 않았습니다.')
        record['widget_trace']['existing_same'] = sum(
            item['title'] == args['title'] and item['time'] == args.get('time')
            for item in response.data['items'])
        record['widget_trace']['duplicate_check_truncated'] = response.data.get('truncated', False)
    elif request.action == 'set':
        args['label'] = '알람'
        args['timezone'] = record['timezone']
    elif spec.target_required:
        query = WidgetRequest(request_id='resolve:' + record['action_key'], widget=request.widget,
                              action='list', args={'start': fact.start or '0001-01-01',
                                                  'end': fact.end or '9999-12-31', 'status': 'all', 'limit': 10000})
        response = await bridge.query(query, record)
        if response.status != 'success':
            raise ProtocolFault('error', 'TARGET_READ_FAILED', '실제 대상을 조회하지 못했습니다. 변경하지 않았습니다.')
        data = response.data
        selected = resolve_target(data['items'], frame.target_text or '', truncated=data.get('truncated', False))
        record['widget_trace']['entity_resolution'] = selected.evidence()
        if selected.status != 'resolved':
            messages = {'ambiguous': '제목에 일치하는 대상이 여러 개입니다. 정확한 제목과 날짜로 다시 요청하세요.',
                        'incomplete': '조회 결과가 잘려 대상의 유일성을 확인할 수 없습니다. 날짜를 지정해 주세요.',
                        'missing': '일치하는 대상이 없습니다. 정확한 제목과 날짜를 확인해 주세요.'}
            raise ProtocolFault('needs_clarification', 'TARGET_' + selected.status.upper(),
                                messages.get(selected.status, '대상의 정확한 제목을 말씀해 주세요.'), 'target')
        item = selected.matches[0]
        target = {'type': 'item_id', 'value': item['id']}
        if not spec.read_only:
            args['version'] = item['version']
        record['widget_trace']['resolved_target'] = {k: item[k] for k in ('id', 'title', 'version', 'date', 'time')}
    return WidgetRequest(request_id=record['action_key'], idempotency_key=record['action_key'],
                         widget=request.widget, action=request.action, target=target,
                         args=args, context={'source': 'assistant'})


def format_semantic_list(record, request, data):
    """Render only actual Adapter rows. Range semantics are not recomputed from a label."""
    start, end = data['range']
    noun = '할 일' if request.widget == 'todo' else '일정'
    status = data['status']
    qualifier = {'pending': '남은 ', 'completed': '완료한 ', 'all': ''}[status]
    label = start if start == end else f'{start}~{end}'
    if start == '0001-01-01' and end == '9999-12-31':
        label = '전체 날짜'
    if data.get('period'):
        label += ' ' + {'morning': '오전', 'afternoon': '오후'}[data['period']]
    text = f'{label} {qualifier}{noun}: {data["count"]}개입니다.'
    for row in data['items']:
        text += f'\n{row["date"]} {row["time"] or "시간 미지정"} · {row["title"]}'
        if row['completed']:
            text += ' [완료]'
    if data.get('untimed_count'):
        text += f'\n시간 미지정 항목 {data["untimed_count"]}개는 오전·오후를 판단할 수 없어 제외했습니다.'
    if data['truncated']:
        text += '\n처음 50개를 표시했습니다. 전체 목록은 할 일 위젯에서 확인하세요.'
    record['routing']['resolved_context'] = {'range': data['range'], 'status': status,
        'period': data.get('period'), 'timezone': record['timezone'],
        'date_policy': 'semantic_source_spans_or_documented_read_default'}
    return text
