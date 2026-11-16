"""Food adapter/agent/speech contracts using isolated DBs and fake providers."""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app import llm_display
from app.food import FoodCreate, FoodUpdate
from app.personal_agent import AGENT_PLAN_SCHEMA, AgentCommandInput, PersonalAgentService
from app.widget_protocol import ExecutionContext
from test_personal_agent import build_test_harness
from test_agent_speech_safety import speech_app, upload, poll_job, settled_agent


AT = '2026-11-16T00:00:00Z'
ADD = '소비기한 알리미에 우유 2개, 소비기한 11월 20일로 추가해 줘'


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 10))


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_LLM_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HUB_AI_PROFILE', 'legacy')
    for key in ('HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED',
                'HUB_OPENAI_AUDIO_ENABLED', 'HUB_RUN_LIVE_OPENAI_TESTS'):
        monkeypatch.setenv(key, 'false')
    monkeypatch.setattr('app.personal_agent.utcnow', lambda: AT)


@pytest.fixture
def agent(tmp_path):
    return build_test_harness(tmp_path, enable_cloud=False, enable_audio_cloud=False)[4]


def command(agent, text=ADD, request='food-command-001', **kwargs):
    return run(agent.submit_command(AgentCommandInput(request_id=request, text=text, **kwargs)))


def insert(agent, name='우유', expiry='2026-11-20', request='food-fixture-001', quantity=2):
    return agent.food.create(FoodCreate(name=name, quantity=quantity, expiry_date=expiry, request_id=request))


def place_food(agent, present=True):
    layout = agent.store.get('layout')
    layout['widgets'] = [w for w in layout['widgets'] if w['type'] != 'food-expiry']
    if present:
        layout['widgets'].append({'id': 'food-card', 'type': 'food-expiry', 'title': '',
                                 'x': 0, 'y': 0, 'w': 4, 'h': 2, 'config': {}})
    agent.store.set('layout', layout)


def test_add_confirmation_and_replay_are_exact_and_durable(agent):
    pending = command(agent, mode='local_only')
    assert pending['status'] == 'awaiting_confirmation'
    assert pending['tier_used'] == 'tier0_local_rule'
    assert pending['model_used'] == 'local/deterministic'
    assert agent.food.items() == []
    assert pending['preview']['mutations'][0]['payload'] == {
        'name': '우유', 'quantity': 2, 'expiry_date': '2026-11-20'}
    repeated = command(agent, mode='local_only')
    assert repeated['duplicate'] and repeated['id'] == pending['id']
    with pytest.raises(HTTPException) as error:
        run(agent.confirm_run(pending['id'], 'wrong-hash'))
    assert error.value.status_code == 409 and agent.food.items() == []
    confirmed = run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert confirmed['status'] == 'succeeded'
    assert confirmed['execution']['mutation_count'] == 1
    assert len(agent.food.items()) == 1 and agent.food.items()[0]['quantity'] == 2
    replayed = run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert replayed['execution']['duplicate'] and len(agent.food.items()) == 1
    assert agent.governor.summary()['call_count'] == 0


def test_local_list_get_update_delete_reuses_exact_rows(agent):
    item = insert(agent)
    listed = command(agent, '소비기한 목록 보여줘', 'food-list-001')
    assert listed['status'] == 'succeeded'
    assert '우유 · 2개 · 소비기한 2026-11-20' in listed['summary_text']
    fetched = command(agent, '우유 소비기한 알려줘', 'food-get-001')
    assert fetched['preview']['reads_executed'][0]['data']['id'] == item['id']
    pending = command(agent, '소비기한 알리미 우유 개수를 3개로 수정해', 'food-update-001')
    assert pending['status'] == 'awaiting_confirmation'
    lock = pending['preview']['mutations'][0]['target_lock']
    assert lock['id'] == item['id'] and lock['table'] == 'hub_food_items'
    run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert agent.food.get(item['id'])['quantity'] == 3
    assert agent.food.get(item['id'])['expiry_date'] == item['expiry_date']
    pending = command(agent, '소비기한 알리미에서 우유 삭제해', 'food-delete-001')
    assert pending['status'] == 'awaiting_confirmation' and len(agent.food.items()) == 1
    run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert agent.food.items() == []


def test_stale_deleted_or_changed_targets_never_retarget(agent):
    item = insert(agent)
    pending = command(agent, '소비기한 알리미 우유 개수를 3개로 수정해', 'food-stale-001')
    agent.food.update(item['id'], FoodUpdate(name='우유', quantity=4, expiry_date=item['expiry_date'], version=1))
    with pytest.raises(HTTPException) as error:
        run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert error.value.status_code == 409 and agent.food.get(item['id'])['quantity'] == 4
    deletion = command(agent, '소비기한 알리미 우유 삭제해', 'food-stale-delete')
    agent.food.delete(item['id'], 2)
    replacement = insert(agent, request='food-replacement')
    with pytest.raises(HTTPException) as error:
        run(agent.confirm_run(deletion['id'], deletion['preview_sha256']))
    assert error.value.status_code == 409
    assert agent.food.items()[0]['id'] == replacement['id']


def test_duplicate_names_do_not_merge_or_use_new_date_to_pick_target(agent):
    first = insert(agent)
    insert(agent, expiry='2026-11-25', request='food-other-batch')
    for index, text in enumerate((
        '소비기한 알리미 우유 삭제해',
        '소비기한 알리미 우유 소비기한을 11월 20일로 수정해',
        '우유 소비기한 알려줘',
    )):
        result = command(agent, text, f'food-ambiguous-{index}')
        assert result['status'] == 'needs_clarification' and result['preview'] is None
    pending = command(agent, '소비기한 11월 20일인 우유 삭제해', 'food-precise-delete')
    assert pending['status'] == 'awaiting_confirmation'
    assert pending['preview']['mutations'][0]['target_lock']['id'] == first['id']
    run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert len(agent.food.items()) == 1 and agent.food.items()[0]['expiry_date'] == '2026-11-25'


@pytest.mark.parametrize('text', [
    '소비기한 알리미 우유 등록해',
    '소비기한 알리미 우유 소비기한 바꾸지 말고 개수만 3개로 수정해',
    '소비기한 알리미 우유 2개 11월 31일까지 추가해',
    '소비기한 알리미 우유 2개 내일까지 추가하고 4분 타이머 시작',
    '소비기한 알리미 우유 2개와 두유 3개 내일까지 추가해',
    '소비기한 알리미에 가능하면 우유 2개 소비기한 내일 등록해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 안등록해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 대신 추가해 줘',
    '소비기한 알리미에 우유 2개 소비기한 내일 다른 것으로 추가해 줘',
    '내일 소비기한 알리미에 우유 2개 등록해 줘',
])
def test_incomplete_or_unsupported_request_never_writes_or_bills(agent, text):
    result = command(agent, text)
    assert result['status'] == 'needs_clarification'
    assert result['preview'] is None and agent.food.items() == []
    assert agent.governor.summary()['call_count'] == 0


@pytest.mark.parametrize('override', [
    {'quantity': 99}, {'quantity': True}, {'expiry_date': '2026-11-21'},
    {'title': '치즈'}, {'action': 'delete'}, {'target_id': 'invented-id'},
    {'status_filter': 'pending'}, {'date': '2026-11-20'},
])
def test_model_food_proposal_cannot_change_source_fields(agent, override):
    step = PersonalAgentService._blank_step(domain='food', action='add', title='우유', quantity=2,
                                          expiry_date='2026-11-20') | override
    with pytest.raises(ValueError):
        run(agent._ground_and_stage_steps('synthetic-proposal', [step],
            agent.build_workspace_snapshot(AT), source_text=ADD))
    assert agent.food.items() == []


def test_forged_food_id_and_mixed_model_plan_are_rejected_before_other_controls(agent):
    intended = insert(agent)
    other = insert(agent, name='치즈', request='food-different-name')
    step = PersonalAgentService._blank_step(domain='food', action='delete', target_title='우유', target_id=other['id'])
    with pytest.raises(ValueError):
        run(agent._ground_and_stage_steps('synthetic-id', [step],
            agent.build_workspace_snapshot(AT), source_text='소비기한 알리미 우유 삭제해'))
    step['target_id'] = intended['id']
    timer = PersonalAgentService._blank_step(domain='timer', action='start', duration_seconds=240)
    with pytest.raises(ValueError):
        run(agent._ground_and_stage_steps('synthetic-mixed', [timer, step],
            agent.build_workspace_snapshot(AT), source_text='4분 타이머 시작하고 소비기한 알리미 우유 삭제해'))
    assert agent.timers.snapshot()['active_count'] == 0 and len(agent.food.items()) == 2


def test_fake_openai_planner_uses_strict_food_schema_without_inventory_catalog(tmp_path):
    calls = []

    async def provider(method, url, headers, body):
        calls.append(url)
        assert url.endswith('/chat/completions')
        schema = body['response_format']['json_schema']
        assert schema['strict'] and schema['schema'] == AGENT_PLAN_SCHEMA
        step_schema = schema['schema']['properties']['steps']['items']
        assert set(step_schema['properties']) == set(step_schema['required'])
        assert step_schema['additionalProperties'] is False
        assert 'quantity' in step_schema['properties'] and 'expiry_date' in step_schema['properties']
        prompt = body['messages'][0]['content']
        assert 'PRIVATE_FOOD_ABC' not in prompt and '1999-06-05' not in prompt
        return {'choices': [{'message': {'content': json.dumps({
            'intent_type': 'action_plan', 'assistant_message': '변경 미리보기를 준비합니다.',
            'follow_up_question': None, 'steps': [PersonalAgentService._blank_step(
                domain='food', action='add', title='우유', quantity=2, expiry_date='2026-11-20')]
        }, ensure_ascii=False)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}

    agent = build_test_harness(tmp_path, transport=provider, api_key='synthetic-no-network')[4]
    insert(agent, name='PRIVATE_FOOD_ABC', expiry='1999-06-05')
    snapshot = agent.build_workspace_snapshot(AT, for_cloud=True)
    assert 'PRIVATE_FOOD_ABC' not in json.dumps(snapshot, ensure_ascii=False)
    result = command(agent, mode='fast')
    assert result['status'] == 'awaiting_confirmation' and len(calls) == 1
    assert len(agent.food.items()) == 1
    run(agent.confirm_run(result['id'], result['preview_sha256']))
    assert len(agent.food.items()) == 2


@pytest.mark.parametrize('intent,with_step', [
    ('action_plan', True), ('general_answer', False), ('briefing', False),
    ('action_plan', False), ('clarification', False),
])
def test_model_narrative_and_empty_plans_cannot_fabricate_food_facts(tmp_path, monkeypatch, intent, with_step):
    async def provider(method, url, headers, body):
        return {'choices': [{'message': {'content': json.dumps({
            'intent_type': intent,
            'assistant_message': '우유는 999개 있으며 소비기한은 2099-01-01입니다.',
            'follow_up_question': 'SECRET_INVENTED_FOOD를 삭제할까요?',
            'steps': [PersonalAgentService._blank_step(domain='food', action='list')] if with_step else [],
        }, ensure_ascii=False)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}

    agent = build_test_harness(tmp_path, transport=provider, api_key='synthetic-no-network')[4]
    insert(agent)
    place_food(agent)

    async def forbidden_briefing(*args, **kwargs):
        raise AssertionError('Food source must not be diverted to a model briefing.')

    monkeypatch.setattr(agent, '_run_briefing', forbidden_briefing)
    result = command(agent, '소비기한 목록 보여줘', mode='fast')
    assert result['status'] == ('succeeded' if with_step else 'needs_clarification')
    for text in (result['summary_text'], json.dumps(llm_display.project_agent_run(agent.store, result), ensure_ascii=False)):
        assert '999개' not in text and '2099-01-01' not in text and 'SECRET_INVENTED_FOOD' not in text
    if with_step:
        assert '우유 · 2개 · 소비기한 2026-11-20' in result['summary_text']
    else:
        assert result['preview'] is None


def test_hidden_food_widget_blocks_lookup_oracle_before_any_target_read(agent, monkeypatch):
    insert(agent)
    place_food(agent, False)

    def forbidden_lookup():
        raise AssertionError('Hidden inventory must not be inspected for paired callers.')

    monkeypatch.setattr(agent.food, 'items', forbidden_lookup)
    caller = ExecutionContext(principal='display:synthetic', role='display', permissions=frozenset({'read', 'control'}))
    messages = set()
    for name in ('우유', '치즈'):
        for text in (f'{name} 소비기한 알려줘', f'소비기한 알리미 {name} 삭제해',
                     f'소비기한 알리미 {name} 개수를 3개로 수정해'):
            plan = agent._try_tier0_plan(text, AT, 'Asia/Seoul')
            with pytest.raises(ValueError) as error:
                run(agent._ground_and_stage_steps('synthetic-hidden-food', plan['steps'],
                    agent.build_workspace_snapshot(AT), source_text=text, caller_auth=caller))
            messages.add(str(error.value))
    assert len(messages) == 1
    assert '위젯을 공유 화면에 배치' in messages.pop()


def test_food_shared_read_rechecks_layout_and_row_version(agent):
    item = insert(agent)
    result = command(agent, '우유 소비기한 알려줘')
    assert llm_display.project_agent_run(agent.store, result)['redacted'] is True
    place_food(agent)
    assert llm_display.project_agent_run(agent.store, result)['redacted'] is False
    agent.food.update(item['id'], FoodUpdate(name='우유', quantity=4, expiry_date=item['expiry_date'], version=1))
    assert llm_display.project_agent_run(agent.store, result)['redacted'] is True
    current = command(agent, '소비기한 목록 보여줘', 'food-current-list')
    assert not llm_display.project_agent_run(agent.store, current)['redacted']
    place_food(agent, False)
    projected = llm_display.project_agent_run(agent.store, current)
    assert projected['redacted'] and '우유' not in projected['output']


@pytest.mark.parametrize('change', ['edit', 'delete', 'hide'])
def test_derived_food_reply_rechecks_live_ancestor_proof(agent, change):
    item = insert(agent, name='SYNTHETIC_PRIVATE_FOOD')
    place_food(agent)
    layout = agent.store.get('layout')
    layout['widgets'].append({'id': 'food-reply', 'type': 'llm-response', 'x': 4, 'y': 0, 'w': 4, 'h': 2, 'config': {}})
    agent.store.set('layout', layout)
    original = command(agent, 'SYNTHETIC_PRIVATE_FOOD 소비기한 알려줘', 'food-original')
    replies = [original]
    for listed in (False, True):
        previous = replies[-1]
        derived = command(agent, '오늘 할 일 확인', f'food-derived-{listed}')
        data = {'id': previous['id'], 'status': 'succeeded', 'redacted': False,
                'summary_text': previous['summary_text']}
        if listed:
            data = {'recent_agent_runs': [data], 'agent_run_count': 1}
        proof = {'mutations': [], 'reads_executed': [{
            'domain': 'llm-response', 'action': 'list' if listed else 'read', 'data': data,
        }]}
        with agent.store.connect() as db:
            db.execute('UPDATE hub_agent_runs SET source_text=?,summary_text=?,preview_json=? WHERE id=?',
                (original['source_text'], original['summary_text'], json.dumps(proof), derived['id']))
        replies.append(agent.get_run(derived['id']))
    assert all(not llm_display.entry(agent.store, reply['id'])['redacted'] for reply in replies)
    if change == 'edit':
        agent.food.update(item['id'], FoodUpdate(name=item['name'], quantity=3,
            expiry_date=item['expiry_date'], version=item['version']))
    elif change == 'delete':
        agent.food.delete(item['id'], item['version'])
    else:
        place_food(agent, False)
    for reply in replies:
        projected = llm_display.entry(agent.store, reply['id'])
        assert projected['redacted']
        assert item['name'] not in json.dumps(projected, ensure_ascii=False)
        assert item['name'] in agent.get_run(reply['id'])['summary_text']


def test_mutation_preview_and_receipt_are_not_broadcast_as_inventory_history(agent):
    place_food(agent)
    pending = command(agent)
    projected = llm_display.project_agent_run(agent.store, pending)
    assert projected['redacted'] and '우유' not in projected['input']
    confirmed = run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert llm_display.project_agent_run(agent.store, confirmed)['redacted']


def test_confirmation_rolls_back_food_and_receipt_on_late_failure(agent, monkeypatch):
    pending = command(agent)
    execute = agent.registry.execute

    async def fail_after_food(request, authority):
        result = await execute(request, authority)
        if request.widget == 'food' and authority.confirmed_digest:
            assert result.status == 'success'
            raise RuntimeError('synthetic late failure')
        return result

    monkeypatch.setattr(agent.registry, 'execute', fail_after_food)
    with pytest.raises(RuntimeError, match='synthetic late failure'):
        run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert agent.food.items() == []
    with agent.store.connect() as db:
        assert db.execute('SELECT count(*) FROM hub_food_receipts').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM hub_agent_receipts').fetchone()[0] == 0
    assert agent.get_run(pending['id'])['status'] == 'awaiting_confirmation'
    monkeypatch.setattr(agent.registry, 'execute', execute)
    run(agent.confirm_run(pending['id'], pending['preview_sha256']))
    assert len(agent.food.items()) == 1


def test_paired_openai_speech_stages_food_once_and_requires_manager_confirmation(speech_app, monkeypatch):
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')

    async def provider(method, url, headers, payload):
        assert method == 'POST' and url == 'https://api.openai.com/v1/audio/transcriptions'
        assert payload[:4] == b'RIFF'
        return {'text': ADD, 'usage': {'input_tokens': 16, 'output_tokens': 8, 'total_tokens': 24}}

    with speech_app(cloud_enabled=True, audio_cloud_enabled=True,
                    local_enabled=False, transport=provider) as h:
        prompts = []
        transcribe = h.agent.gateway.transcribe_audio

        async def inspect_hints(**kwargs):
            prompts.append(kwargs['prompt_hint'])
            return await transcribe(**kwargs)

        monkeypatch.setattr(h.agent.gateway, 'transcribe_audio', inspect_hints)
        place_food(h.agent)
        submitted = upload(h, 'food-paired-openai-once')
        assert submitted.status_code == 202, submitted.text
        job = poll_job(h, submitted.json()['id'], settled_agent)
        view = job['agent_run']
        assert job['stt_backend'] == 'openai'
        assert view['status'] == 'awaiting_confirmation' and view['device_id'] == h.device_id
        assert h.agent.food.items() == [] and h.runner.calls == []
        assert len(h.provider_calls) == 1 and len(prompts) == 1
        assert '소비기한' in prompts[0]
        stored = h.agent.get_run(view['id'])
        assert stored['principal'] == 'display:' + h.device_id
        confirm_path = '/api/agent/runs/' + view['id'] + '/confirm'
        body = {'preview_sha256': stored['preview_sha256']}
        assert h.client.post(confirm_path, json=body).status_code in {401, 403}
        confirmed = h.client.post(confirm_path, json=body, headers=h.admin)
        assert confirmed.status_code == 200, confirmed.text
        assert len(h.agent.food.items()) == 1 and h.agent.food.items()[0]['name'] == '우유'
        repeated = upload(h, 'food-paired-openai-once')
        assert repeated.status_code == 202 and repeated.json()['id'] == job['id']
        assert repeated.json()['duplicate'] is True
        assert h.client.post(confirm_path, json=body, headers=h.admin).json()['execution']['duplicate']
        assert len(h.agent.food.items()) == 1 and len(h.provider_calls) == 1
        assert h.client.put('/api/food/items/' + h.agent.food.items()[0]['id'], json={
            'name': '우유', 'quantity': 99, 'expiry_date': '2026-11-20', 'version': 1}).status_code in {401, 403}
