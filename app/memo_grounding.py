"""Bounded named-memo plans: raw source -> metadata catalog -> existing Adapter.

Current/latest FAST_PATH stays independent. No body search, fuzzy matching,
active-card guesses, model-owned IDs or implicit conversion of update to create.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import re

from .entity_catalog import EntityCatalog
from .entity_resolver import normalize_title
from .life import note_catalog_snapshot
from .widget_protocol import WidgetRequest
from .widget_protocol.core import ProtocolFault

MEMO_GROUNDING_VERSION = '1.1.0'
from .memo_language import NOUN, PREFIX, recognize, grammar_hash

# These are selectors/commands/filters, not literal title evidence. Keep the
# ordinary current/latest and explicit-ID paths delegated to the existing core.
DELEGATED = re.compile(r'^(?:현재|지금|방금|아까|최근|마지막|새|기본)(?:\s|$)')
ID = re.compile(r'(?<!\w)(?:ID|아이디)(?:\s|$)', re.I)
UNSUPPORTED_TITLE = re.compile(
    r'관련|포함|들어간|검색|찾아|그중|중에서|중에|제외|빼고|나머지|전부|전체|모두|모든|'
    r'첫\s*번째|두\s*번째|\d+\s*번째|여러|한\s*번에|그거|그것|이거|저거|'
    r'^(?:오늘|내일|모레|어제)(?:\s|$)|부터|까지|이후|이전')
MIXED_TITLE = re.compile(r'(?:할\s*일|일정|스케줄|알람|타이머)(?:과|와|랑|하고)\s+|그리고')


@dataclass(frozen=True, slots=True)
class MemoPlan:
    action: str
    target_text: str
    arguments: tuple[tuple[str, str], ...] = ()
    evidence: tuple[tuple[str, int, int, str], ...] = ()
    issue: str | None = None
    family: str = 'named.literal'
    grammar_sha256: str = ''
    version: str = MEMO_GROUNDING_VERSION

    def data(self):
        value = asdict(self)
        value['arguments'] = dict(self.arguments)
        value['evidence'] = [{'field': key, 'start': start, 'end': end, 'text': text,
                              'scope': 'raw_source_span'} for key, start, end, text in self.evidence]
        return value

    def proposal(self):
        return {'widget': 'memo', 'action': self.action, 'target': None, 'args': dict(self.arguments)}


def parse_memo(raw: str) -> MemoPlan | None:
    """Keep literal body whitespace; this grammar matches RAW, not rewritten text."""
    from .command_routing import unsafe_source
    if not isinstance(raw, str) or len(raw) > 1500:
        return None
    source = re.sub(r'[?？!！.。]+$', '', raw).rstrip()
    if (unsafe_source(raw) or re.search(r'[\r\n\x00-\x1f]|["“”‘’\'`]', raw)
            or not re.search(NOUN, source)):
        return None  # Existing global unsafe checks remain authoritative.
    match = recognize(raw)
    if match is None or match.family.disposition == 'delegate':
        return None
    if match.family.disposition == 'block':
        return MemoPlan(match.family.action, '', evidence=match.captures, issue=match.family.issue,
                        family=match.family.name, grammar_sha256=grammar_hash())
    title = normalize_title(match.value('title') or '')
    if match.family.name.startswith('named.literal.') and (DELEGATED.search(title) or ID.search(title)):
        return None
    issue = None
    if (not title or title in {'그', '이', '저'} or len(title) > 120 or UNSUPPORTED_TITLE.search(title)
            or MIXED_TITLE.search(title) or re.search(NOUN, title)):
        issue = 'MEMO_NAMED_CONSTRAINT_UNSUPPORTED'
    # "X라고 써둔" can describe BODY, not necessarily a note name. Do not
    # remove arbitrary quoted/reporting material to manufacture a title.
    if re.search(r'(?:이)?라고|(?:이)?라는', title):
        issue = 'MEMO_NAMING_AMBIGUOUS'
    args = tuple((key, match.value(key)) for key in ('body', 'text') if match.value(key) is not None)
    if any(not value.strip() for _, value in args):
        issue = 'MEMO_CONTENT_REQUIRED'
    return MemoPlan(match.family.action, title, args, match.captures, issue,
                    match.family.name, grammar_hash())


def has_unhandled_name(raw: str) -> bool:
    """Fallback must not silently replace a spoken title with current/last.

    This is a refusal guard, NOT a second target extractor. Only parse_memo can
    authorize a named source plan. Existing selectors, new-note syntax and IDs
    remain owned by the old protocol path.
    """
    match = recognize(raw)
    if match and match.family.disposition == 'delegate':
        return False  # Whole selector/content-first family, NOT a keyword anywhere.
    if match and match.family.disposition in {'named', 'block'} and not match.family.name.startswith('named.literal.'):
        return True  # Explicit naming cannot be replaced by current/latest.
    prefix = re.match(PREFIX, raw.strip())
    if not prefix:
        return False
    title = normalize_title(prefix['title'])
    return bool(title and not DELEGATED.search(title) and not ID.search(title))


def prepare_memo(bridge, record):
    match = recognize(record['raw'])
    if match is not None:
        record['memo_language'] = match.audit() | {'claimed': False, 'scope': 'linguistic_candidate; actual_route_authoritative'}
    if record.get('fast_read'):
        return None  # Never reinterpret current/latest as a named catalog item.
    plan = parse_memo(record['raw'])
    if plan is None:
        return None
    if match is not None:
        record['memo_language']['claimed'] = True
    from .assistant import digest
    from .widget_bridge import BRIDGE_VERSION
    adapter = bridge.registry.get('memo')
    spec = adapter.operations.get(plan.action) if adapter else None
    if spec is None:
        return None
    cap = next(c for c in bridge.registry.get_capabilities('memo') if c.action == plan.action)
    record.update(route='rule', origin='server', state='new', final_text=None, proposal=None,
                  _widget_domain_request=True, widget_bridge=BRIDGE_VERSION,
                  memo_read=True, protocol_private=not spec.read_only,
                  memo_plan=plan.data(), direct_widget=plan.proposal())
    record['routing'].update(route='MEMO_GUARD' if plan.issue else 'ENTITY_CATALOG',
                             route_reason=plan.family + ('.blocked' if plan.issue else ''),
                             resolved_intent='memo.' + plan.action, confidence='UNSUPPORTED' if plan.issue else 'EXACT')
    record['widget_trace'] = {
        'domain': 'memo', 'candidate_domains': ['memo'], 'available_capabilities': ['memo.' + plan.action],
        'capability_fingerprint': digest([['memo', cap.model_dump(mode='json')]]),
        'schema_validation': 'not_run', 'policy_result': 'not_run', 'widget_request': None,
        'widget_response': None, 'adapter': None, 'source_of_truth': None,
        'memo_grounding_version': MEMO_GROUNDING_VERSION, 'source_evidence': plan.data(),
        'latency_ms': {'stt_ms': None, 'router_ms': 0., 'llm_ms': 0., 'adapter_ms': 0., 'total_ms': None}}
    return record


def verified_plan(record, *, db=None, rid=None):
    from .assistant import normalize
    plan = parse_memo(record['raw'])
    if (normalize(record['raw'])[0] != record['normalized'] or plan is None
            or plan.data() != record.get('memo_plan') or plan.proposal() != record.get('direct_widget')):
        raise ProtocolFault('needs_clarification', 'MEMO_SOURCE_CHANGED', '메모 요청 원문과 계획이 달라졌습니다. 새 요청으로 확인하세요.')
    if db is not None:
        from .llm import sha
        row = db.execute('SELECT source_text,source_sha256 FROM llm_requests WHERE id=?', (rid,)).fetchone()
        if row is None or row['source_text'] != record['raw'] or row['source_sha256'] != sha(record['raw']):
            raise ProtocolFault('needs_clarification', 'MEMO_SOURCE_CHANGED', '저장된 메모 요청 원문을 확인할 수 없습니다.')
    return plan


def catalog_matches_response(record, response):
    """A rename/update between catalog read and body read cannot change identity."""
    selected = record['widget_trace'].get('resolved_target')
    data = response.data
    if response.status == 'success' and (not selected or any(data.get(k) != selected[k] for k in ('id', 'title', 'version'))):
        raise ProtocolFault('needs_clarification', 'MEMO_TARGET_CHANGED', '조회 중 메모가 변경되었습니다. 다시 요청하세요.')


async def ground_memo(bridge, record, request, spec):
    plan = verified_plan(record)
    trace = record['widget_trace']
    trace['execution_eligibility'] = {'decision': 'BLOCKED', 'authority': 'existing_registry_and_confirmation_only'}
    if request.widget != 'memo' or request.action != plan.action or request.args != dict(plan.arguments) or request.target is not None:
        raise ProtocolFault('needs_clarification', 'MEMO_PLAN_CHANGED', '원문에서 확인한 메모 요청만 처리합니다.')
    if plan.issue:
        raise ProtocolFault('needs_clarification', plan.issue, '검색·복합·순서 조건을 빼고 실행하지 않습니다. 메모의 정확한 제목과 한 동작을 요청하세요.')
    if not spec.available:
        raise ProtocolFault('unavailable', 'CAPABILITY_UNAVAILABLE', '현재 사용할 수 없는 메모 동작입니다.')
    try:
        snapshot = await asyncio.to_thread(note_catalog_snapshot, bridge.store)
        catalog = EntityCatalog.from_rows('memo', snapshot['items'], source=snapshot['source'], as_of=snapshot['as_of'],
            truncated=snapshot['truncated'], scope={'selection': 'named_title', 'sharing': 'all_manager_visible',
                                                 'revision': str(snapshot['revision'])})
        selected = catalog.resolve(plan.target_text)
    except Exception:
        # A storage/shape error is not an empty catalog or permission to fallback.
        raise ProtocolFault('error', 'CATALOG_READ_FAILED', '메모 목록을 읽지 못했습니다. 이름을 추측하거나 다른 메모를 사용하지 않았습니다.') from None
    trace['entity_catalog'] = catalog.evidence()
    trace['entity_resolution'] = selected.evidence()
    if selected.status != 'resolved':
        messages = {'missing': '제목에 일치하는 메모가 없습니다. 정확한 제목을 확인하세요.',
                    'ambiguous': '같은 제목에 해당하는 메모가 여러 개입니다. 관리자에서 대상을 확인하세요.',
                    'incomplete': '메모 목록이 잘려 대상을 확정할 수 없습니다. 관리자에서 확인하세요.'}
        raise ProtocolFault('needs_clarification', 'MEMO_TARGET_' + selected.status.upper(), messages[selected.status], 'target')
    item = selected.matches[0]
    trace['resolved_target'] = {k: item[k] for k in ('id', 'title', 'version')}
    target = {'type': 'item_id', 'value': item['id']}
    args = dict(plan.arguments)
    if not spec.read_only:
        # Read only the selected body through the existing authorized Adapter.
        response = await bridge.query(WidgetRequest(request_id='resolve:' + record['action_key'],
                                                    widget='memo', action='read', target=target), record)
        if response.status != 'success':
            raise ProtocolFault('needs_clarification', 'MEMO_TARGET_CHANGED', '변경할 메모를 다시 확인해 주세요.')
        catalog_matches_response(record, response)
        args['version'] = item['version']
    trace['execution_eligibility'] = {'decision': 'READY_FOR_EXISTING_POLICY', 'reason_code': 'SOURCE_AND_TARGET_CHECKED',
        'read_only': spec.read_only, 'authority': 'existing_registry_and_confirmation_only'}
    return WidgetRequest(request_id=record['action_key'], idempotency_key=record['action_key'],
                         widget='memo', action=plan.action, target=target, args=args, context={'source': 'assistant'})
