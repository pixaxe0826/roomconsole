"""Four tiny, generated contract cases; NOT the user's evaluation/holdout dataset."""
from pathlib import Path
import json


def make_suite(root: Path, name: str = 'synthetic') -> Path:
    path = root / name
    path.mkdir(parents=True)
    def save(name, value):
        (path / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    manifest = {'version': 'test-1', 'reference_datetime': '2026-09-21T09:00:00+09:00',
                'timezone': 'Asia/Seoul', 'case_files': ['cases.jsonl'],
                'case_count': 4, 'unique_instruction_count': 3, 'paraphrase_count': 1,
                'projection_file': 'projection.json'}
    fixtures = {'memos': [{'id': 'synthetic-note', 'title': 'Unit test', 'body': 'Generated fixture only',
                            'pinned': True, 'shared': True}],
                'layout': {'columns': 8, 'rows': 6, 'version': 1, 'widgets': [
                    dict(id='test-note', type='note', title='Unit note', x=0, y=0, w=4, h=2, config={}),
                    dict(id='test-timer-a', type='timers', title='A', x=0, y=2, w=2, h=2, config={}),
                    dict(id='test-timer-b', type='timers', title='B', x=2, y=2, w=2, h=2, config={})]}}
    def case(identity, text, cap, policy, status, slots, execute=False, **kwargs):
        return dict(id=identity, input_text=text, source_type='unique', expected=dict(
            capability=cap, domain=cap.split('.')[0], policy=policy, status=status,
            slots=slots, adapter_should_execute=execute), **kwargs)
    rows = [case('TEST001', '내일 할 일에 합성 항목 추가해', 'todo.add', 'CONFIRM', 'needs_confirmation', {'date': '2026-09-22'}),
            case('TEST002', '그 항목의 날짜를 정해 줘', 'todo.list', 'CLARIFY', 'needs_clarification', {'date': None}),
            case('TEST003', '현재 메모 읽어줘', 'memo.read', 'EXECUTE', 'success', {'memo_id': 'synthetic-note'}, True),
            case('TEST004', '내일 할 일에 합성 항목을 추가해 주세요', 'todo.add', 'CONFIRM', 'needs_confirmation', {'date': '2026-09-22'})]
    rows[3].update(source_type='paraphrase', parent_id='TEST001')
    save('manifest.json', manifest)
    save('fixtures.json', fixtures)
    save('schema.json', {'slot_schemas': {
        'todo.add': {'type': 'object', 'properties': {'date': {'type': ['string', 'null'], 'format': 'date'}}},
        'memo.read': {'type': 'object'}}, 'fixture_slot_references': ['memo_id']})
    save('projection.json', {'capability_aliases': {'todo.create': 'todo.add'}, 'target_slots': {'memo': 'memo_id'}})
    (path / 'cases.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    return path
