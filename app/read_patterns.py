"""Anchored read-only Korean grammar; deliberately not fuzzy STT correction."""
# This table is separate from context/DB code so variations can be tested without I/O.
MEMO_END = (r'(?:읽어\s*(?:줘|주세요)|보여\s*(?:줘|주세요)|'
            r'알려\s*(?:줘|주세요)|확인해\s*(?:줘|주세요))(?:요)?')
MEMO_PATTERNS = (
    ('memo.latest', 'last_modified',
     r'(?:방금|아까|최근(?:에)?)\s*(?:(?:적은|작성한|수정한|저장한)\s*메모(?:\s*(?:내용|본문))?|'
     r'메모(?:에\s*(?:작성한|적은|저장한|수정한)\s*내용)?)(?:을|를)?\s*(?:좀\s*)?' + MEMO_END),
    ('memo.current', 'current',
     r'(?:(?:현재|지금)\s*)?메모(?:에\s*(?:남아\s*있는|적혀\s*있는|저장된)\s*내용|'
     r'\s*(?:내용|본문))?(?:을|를)?\s*(?:좀\s*)?' + MEMO_END),
    ('memo.question', 'current',
     r'(?:(?:현재|지금)\s*)?메모(?:에)?\s*뭐라고\s*(?:적혀|쓰여)\s*있어(?:요)?'),
)
# Only an explicit time-of-day slot plus a calendar noun and read ending.
# DATE parsing is owned by clock_service, not by these regular expressions.
CALENDAR_PERIOD = (r'(?P<period>오전|오후)\s*(?P<state>남은|미완료|완료된|완료한|전체|모든)?\s*'
                   r'(?:일정|스케줄|달력)(?:을|를|이|은|는|가)?\s*(?:좀\s*)?')

# A dated noun alone is an explicit dashboard query, not a substring guess.
DATED_BRIEF = r'(?P<period>오전|오후)?\s*(?P<noun>할\s*일|일정)'
