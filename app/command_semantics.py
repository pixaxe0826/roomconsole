"""Source evidence shared by rule routing and model proposal validation.

Not ASR repair: never turn '네' into '내일', or fuzzy-match a write target.
"""
from __future__ import annotations
import re
from .clock_service import prefix_date, resolve_source_dates, resolve_range

# Supports '확인해', '확인해줘', '확인해 줘', and non-imperative questions.
READ_END = (r'(?:보여\s*(?:줘|주세요)|알려\s*(?:줘|주세요)|말해\s*(?:줘|주세요)|'
            r'(?:확인|조회|보고|브리핑|정리|요약)(?:해)?(?:\s*(?:줘|주세요|줘요))?|'
            r'확인해서\s*보고해(?:\s*(?:줘|주세요))?|'
            r'뭐\s*(?:남아\s*있어|남았어|있어)|뭐야|뭔가요|뭐\s*있니|'
            r'어떤\s*(?:게|것이)\s*있어|남아\s*있어|있어)(?:요)?')
# Deliberately no '남은 애' / '라면 달릴' repair. Those are ambiguous source text.
READ_NOUN = r'(?:할\s*일(?:\s*목록)?|해야\s*(?:할\s*일|할\s*것|할\s*거|하는\s*일)|남은\s*일|일정|스케줄|달력)'
STATE_PREFIX = r'(?:아직\s*)?(?:남은|미완료(?:인)?|완료된|완료한|끝낸|안\s*끝낸|하지\s*않은|완료하지\s*않은|해야\s*할|모든|전체)?'


def status_evidence(text: str) -> str | None:
    s=re.sub(r'\s+','',text)
    pending=bool(re.search(r'남은|남아있|남았|미완료|안끝낸|하지않은|완료하지않은|해야할|해야돼|해야되|해야하',s))
    # Avoid matching '완료한' inside a negation. A title-based mutation does not
    # use this helper; only read/bulk selection strings are accepted.
    completed=bool(re.search(r'완료(?:된|한)|끝낸|끝난',s)) and not bool(re.search(r'안끝낸|완료하지않',s))
    if pending and completed:raise ValueError('미완료와 완료 조건이 함께 있습니다. 한 조건으로 다시 요청하세요.')
    if pending:return 'pending'
    if completed:return 'completed'
    return None


def known_read(text: str, at: str, tz: str) -> dict | None:
    s=re.sub(r'[?？!！.。]+$','',text).strip()
    ref,rest=prefix_date(s)
    # Boundary comma between a state and task phrase is formatting, not a title edit.
    rest=re.sub(r'^(남은|미완료|완료된|전체|모든)\s*[,，]\s*',r'\1 ',rest)
    if re.search(r'부터|까지|말고|아니|하지\s*마|조건|만약|제외|빼고|말하지|숨겨|;|["“”‘’`\']',rest):return None
    if re.fullmatch(r'(?:나는\s*|내가\s*)?(?:뭐|무엇(?:을)?)\s*(?:해야\s*(?:돼|되나|하나|하지|되니|되나요)|할\s*일(?:이)?\s*있어)(?:요)?',rest):
        if ref is None:return None  # Don't guess a day for "네, 뭐 해야 돼".
        return {'intent':'todo.list','date_ref':ref,'status':'pending','scope':'one'}
    # date + '남은 게 뭐야' is sufficiently scoped in Room Hub automatic mode.
    if ref and re.fullmatch(r'남은\s*(?:게|거|것)\s*(?:뭐야|알려\s*줘|보여\s*줘)(?:요)?',rest):
        return {'intent':'todo.list','date_ref':ref,'status':'pending','scope':'one'}
    pattern=(r'^'+STATE_PREFIX+r'\s*'+READ_NOUN+r'(?:들)?(?:을|를|이|은|는|가)?\s*'
             r'(?:좀\s*|간단히\s*|짧게\s*)?'+READ_END+r'$')
    if not re.fullmatch(pattern,rest):return None
    ranges=resolve_source_dates(s,at,tz)  # also reject invalid dates, never silently default
    if ref is None and ranges:ref=ranges[0].evidence
    if ref is None and re.match(r'^(?:전체|모든)\s*할\s*일',rest):ref='전체'
    return {'intent':'calendar.query' if re.search(r'일정|달력|스케줄',rest) else 'todo.list',
            'date_ref':ref,'status':status_evidence(rest) or 'all','scope':'one'}


def is_personal_task_request(s: str) -> bool:
    return bool(re.search(r'할\s*일|일정|달력|스케줄|남은\s*(?:일|게|거|것|애)|남아\s*있|해야\s*(?:돼|되|하|할)|(?:완료|삭제|지워|추가|등록)',s))


def unsupported_personal(s: str) -> str | None:
    if re.search(r'알람|알림|깨워|깨우|리마인더|타이머',s) and re.search(r'설정|등록|추가|맞춰|울려|해\s*줘|알려\s*줘|깨워|확인|보여|조회',s):
        return '알람 예약·실행 기능은 아직 연결되지 않았습니다. 알람을 설정하거나 울리지 않았습니다.'
    if re.search(r'메모(?!리)',s) and re.search(r'저장|해\s*줘|해$|추가|찾아|검색|보여|조회|삭제|고쳐',s):
        return '현재 메모 위젯은 표시용이며 메모 저장·검색 서비스는 아직 연결되지 않았습니다. 메모는 변경하지 않았습니다.'
    return None


def source_constraints(text: str, at: str, tz: str, *, read=False) -> dict:
    ranges=resolve_source_dates(text,at,tz)
    return {'date':ranges[0].data() if ranges else None,
            'status':status_evidence(text) if read else None}


def match_model_date(proposed: str | None, text: str, at: str, tz: str, *, write: bool) -> tuple[str | None, dict]:
    refs=resolve_source_dates(text,at,tz)
    if not refs:
        if proposed:
            # '전체 할 일' is an explicitly grounded whole-history read.
            flat=re.sub(r'\s+','',text)
            if not write and proposed in {'전체','모든'} and re.search(r'(전체|모든)할일',flat):
                return proposed,resolve_range(proposed,at,tz).data()
            raise ValueError('원문에 없는 날짜를 모델이 제안했습니다. 날짜를 명확히 다시 요청하세요.')
        r=resolve_range(None,at,tz)
        return None,r.data()
    source=refs[0]
    if proposed is None:
        if write: raise ValueError('원문 날짜가 변경 분석에서 누락되었습니다. 날짜를 확인해 다시 요청하세요.')
        return source.evidence,source.data()  # read query constraints are server-owned
    model=resolve_range(proposed,at,tz)
    if (source.start,source.end)!=(model.start,model.end):
        raise ValueError('분석한 날짜가 원문의 날짜와 다릅니다. 작업은 실행하지 않았습니다.')
    # Model can say '2026-09-21' for '내일', but stored canonical expression is
    # still source-grounded, with its resolved range recorded separately.
    return source.evidence,source.data()
