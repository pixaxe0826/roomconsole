"""Conservative countdown grammar; durations never come from model guesses."""
import re

KOREAN = {'한':1, '하나':1, '일':1, '두':2, '둘':2, '이':2, '세':3, '셋':3, '삼':3,
          '네':4, '넷':4, '사':4, '다섯':5, '오':5, '여섯':6, '육':6, '일곱':7, '칠':7,
          '여덟':8, '팔':8, '아홉':9, '구':9, '열':10, '십':10}
NUMBER = r'(?:\d+|' + '|'.join(sorted(KOREAN, key=len, reverse=True)) + ')'
DURATION = re.compile(r'(?:(?P<m>' + NUMBER + r')\s*분\s*)?(?:(?P<s>' + NUMBER + r')\s*초)?')
START = r'(?:시작|가동|설정)(?:해)?|돌려|맞춰|켜'
STOP = r'(?:종료|중지|취소)(?:해)?|멈춰|꺼'
ENDING = r'(?:\s*(?:줘|주세요|줘요))?(?:요)?'


def clean(text):
    return re.sub(r'[.。?!？！]+$', '', text).strip()


def seconds(text):
    m = DURATION.fullmatch(text.strip())
    if not m or not any(m.groups()):
        raise ValueError('타이머 시간을 1초부터 10분 사이로 말씀해 주세요.')
    def number(value):
        return int(value) if value and value.isdecimal() else KOREAN.get(value, 0)
    mins, secs = number(m['m']), number(m['s'])
    if m['m'] and m['s'] and secs >= 60:
        raise ValueError('분과 초를 함께 말할 때 초는 0~59로 지정해 주세요.')
    total = mins*60 + secs
    if not 1 <= total <= 600:
        raise ValueError('타이머는 1~600초(10분) 범위로 시작할 수 있습니다.')
    return total


def source_duration(text):
    # Fallback grounding: exactly one source duration, never negative/decimal or
    # an absolute time/scheduled/doubled number disguised as a countdown.
    if re.search(r'[-+−\d.]\d*\.\d|[-+−]\s*\d|시간|시\s*\d|내일|오늘|오전|오후|아침|저녁|후에|뒤에|마다|반복|전부|모두|전체', text):
        raise ValueError('지금 시작할 한 개의 타이머 시간을 초 또는 분·초로 말씀해 주세요.')
    matches = [m for m in DURATION.finditer(text) if any(m.groups())]
    if len(matches) != 1:
        raise ValueError('타이머의 시간을 하나만 명확히 말씀해 주세요.')
    rest = text[:matches[0].start()] + text[matches[0].end():]
    if re.search(r'\d', rest):
        raise ValueError('다른 숫자가 포함되어 시간을 확정할 수 없습니다.')
    return seconds(matches[0][0])


def exact_timer(text):
    s = clean(text)
    start = re.fullmatch(r'(.+?)\s*타이머(?:를)?\s*(?:' + START + ')' + ENDING, s)
    if start is None:
        start = re.fullmatch(r'타이머(?:를)?\s*(.+?)(?:로)?\s*(?:' + START + ')' + ENDING, s)
    if start:
        # Only whole duration grammar takes the exact branch; ambiguity is not
        # permission for a model to repair an out-of-range duration.
        duration = seconds(start[1])
        return {'widget':'timer', 'action':'start', 'target':None,
                'args':{'duration_seconds':duration}}
    if re.fullmatch(r'타이머(?:를)?\s*(?:' + START + ')' + ENDING, s):
        raise ValueError('몇 초 또는 몇 분 타이머를 시작할까요? 1~600초로 말씀해 주세요.')
    if re.fullmatch(r'(?:현재|지금|마지막)?\s*타이머(?:를)?\s*(?:' + STOP + ')' + ENDING, s):
        return {'widget':'timer', 'action':'stop', 'target':{'type':'reference', 'value':'current'}, 'args':{}}
    by_id = re.fullmatch(r'타이머\s*(?:ID|아이디)\s+([A-Za-z0-9_-]{1,80})(?:를)?\s*(?:' + STOP + ')' + ENDING, s, re.I)
    if by_id:
        return {'widget':'timer', 'action':'stop', 'target':{'type':'item_id', 'value':by_id[1]}, 'args':{}}
    if re.fullmatch(r'(?:현재\s*)?타이머(?:\s*(?:목록|상태|현황|남은\s*시간))?(?:을|를)?\s*(?:확인(?:해)?|알려|보여|조회(?:해)?)' + ENDING, s):
        return {'widget':'timer', 'action':'list', 'target':None, 'args':{}}
    return None


def format_timer(request, data, duplicate=False):
    def duration(n):
        m, s = divmod(n,60)
        return ((f'{m}분 ' if m else '') + (f'{s}초' if s or not m else '')).strip()
    if request.action == 'list':
        running = [x for x in data['items'] if x['state']=='running']
        if not running:
            return '현재 실행 중인 타이머가 없습니다.'
        return f'실행 중인 타이머는 {len(running)}개입니다.\n' + '\n'.join(
            f'{x["label"]}: {duration(x["remaining_seconds"])} 남음' + (' · 현재 타이머' if x['id']==data['current_id'] else '') for x in running)
    if request.action == 'get':
        return f'{data["label"]}: {duration(data["remaining_seconds"])} 남음 · {data["state"]}'
    if duplicate or data.get('duplicate'):
        return '이미 처리한 타이머 요청입니다. 다시 시작하거나 다른 타이머를 종료하지 않았습니다.'
    if request.action == 'start':
        return f'{duration(data["duration_seconds"]).strip()} 타이머를 시작했습니다. ID: {data["id"]}'
    if data.get('changed'):
        return f'타이머를 종료했습니다: {data["label"]} · ID: {data["id"]}'
    return f'이미 끝난 타이머입니다: {data["label"]}. 다른 타이머는 변경하지 않았습니다.'
