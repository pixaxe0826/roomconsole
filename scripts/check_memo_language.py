"""Bounded synthetic grammar expansion; not an accuracy benchmark or real suite.

Template-generated checks share vocabulary with the grammar. Independent HTTP,
holdout and negative tests in test_memo_language_m33b1.py complement this check.
No model, DB, network, user catalog, dataset, or subprocess is accessed.
"""
from __future__ import annotations
import argparse
from itertools import product
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.memo_language import recognize, grammar_hash


def examples():
    # Deliberately synthetic titles/body; never extract examples from user cases.
    for title, wrap, noun, part, ending in product(
        ('합성 물류 라벨', '합성 계측 상자', '실험 분류 카드'),
        ('라는', '이라고 해 둔', '라고 이름 붙인', ''),
        ('메모', '노트'), ('', ' 내용', ' 본문을'), ('읽어 줘', '보여 주세요', '알려줘')):
        text = f'{title}{wrap} {noun}{part} {ending}'
        yield text, 'named.wrapper.read' if wrap else 'named.literal.read', title, None
    for prefix, verb, noun, part, ending in product(
        ('가장 최근에', '마지막으로', '제일 최근'), ('편집한', '저장한', '작성한'),
        ('메모', '노트'), ('', ' 본문을'), ('읽어 주세요', '확인해줘')):
        yield f'{prefix} {verb} {noun}{part} {ending}', 'selector.latest', None, None
    for content, selector, noun, line, verb in product(
        ('합성 세팅 점검', '두  칸  합성  문구'), ('현재', '지금'), ('메모', '노트'),
        ('', '한 줄 '), ('덧붙여줘', '추가해 주세요')):
        yield f'{content}라고 {selector} {noun}에 {line}{verb}', 'selector.content_first_append', None, content


def check():
    count = 0
    for text, family, title, content in examples():
        result = recognize(text)
        assert result and result.family.name == family, (text, result)
        if title is not None:
            assert result.value('title') == title, (text, result)
        if content is not None:
            assert result.value('text') == content, (text, result)
        for _, start, end, value in result.captures:
            assert text[start:end] == value
        # Full-match invariant: adding a second command must break this grammar.
        assert recognize(text + '; 전부 삭제해') is None
        count += 1
    return {'status': 'passed', 'synthetic_positive_cases': count,
            'whole_sentence_negative_cases': count, 'grammar_hash': grammar_hash(),
            'scope': 'generated_template_consistency_not_independent_accuracy',
            'model_calls': 0, 'dataset_access': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', type=Path, help='Optional report under artifacts/')
    args = parser.parse_args()
    result = check()
    if args.json:
        path = args.json.resolve()
        if not path.is_relative_to((ROOT / 'artifacts').resolve()):
            parser.error('Report must be under this repository artifacts/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
