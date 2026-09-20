"""Conservative post-generation checks. No made-up model tokens or answers."""
import re


def compact(s):
    return ''.join(c for c in s if c.isalnum())


def assess(output: str, source: str, finish: str | None) -> dict:
    text=output.strip(); issues=[]
    chars=compact(text)
    if len(chars)>12000: chars=chars[:6000]+chars[-6000:]
    if not text: issues.append('empty_output')
    # Catch only substantial repeated runs (at least 24 chars). Normal JSON or
    # two repeated words do not trigger this; not applied to parser JSON.
    if len(chars)>=24:
        for width in range(2,min(48,len(chars)//6)+1):
            hit=False
            for i in range(len(chars)-width*6+1):
                pattern=chars[i:i+width]
                if len(set(pattern))<2:continue
                n=1
                while chars.startswith(pattern,i+n*width): n+=1
                if n>=6 and n*width>=24:
                    issues.append('repeated_phrase');hit=True;break
            if hit:break
        if not issues:
            lines=[compact(line) for line in text.splitlines() if len(compact(line))>=6]
            if any(lines.count(x)>=4 for x in set(lines)): issues.append('repeated_lines')
    src=compact(source)
    if chars and src and (chars==src or chars==src.rstrip('줘')+'줘요'):
        issues.append('echo')
    if finish=='length':issues.append('output_truncated')
    if '<think>' in text or '</think>' in text:issues.append('unexpected_reasoning')
    return {'ok':not issues,'issues':list(dict.fromkeys(issues)),
            'stage':'post_response','automatic_retry':False}


def safe_message(check):
    if any(i.startswith('repeat') for i in check['issues']):
        return '모델이 같은 표현을 반복하여 답변을 채택하지 않았습니다. 요청을 짧게 나누거나 다시 보내 주세요. 작업은 변경하지 않았습니다.'
    if 'output_truncated' in check['issues']:
        return '응답이 출력 한도에서 끝나 완전한 답변으로 채택하지 않았습니다. 질문을 짧게 나누어 주세요. 작업은 변경하지 않았습니다.'
    return '모델의 답변 품질을 확인하지 못했습니다. 요청을 구체적으로 다시 말해 주세요. 작업은 변경하지 않았습니다.'
