"""GPT-Transcribe contracts using synthetic WAV and fake providers, never ASR scores."""
import asyncio
from email import policy as email_policy
from email.parser import BytesParser
import io
import json

import httpx
import pytest

from app.openai_budget import BudgetGovernor, BudgetPolicy, calculate_cost_microdollars, policy_from_env
from app.openai_gateway import OpenAIGateway, OpenAIGatewayError, normalize_transcription_keywords
from app.store import Store
from app.transcription_hints import room_hub_transcription_hints
from test_personal_agent import build_test_harness, make_wav_bytes


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY', 'HUB_OPENAI_STT_MODEL',
                 'HUB_PERSONAL_AGENT_ENABLED', 'HUB_OPENAI_ENABLED', 'HUB_OPENAI_AUDIO_ENABLED'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HUB_AI_PROFILE', 'odyssey_openai')


@pytest.fixture
def governor(tmp_path):
    gov = BudgetGovernor(Store(tmp_path / 'synthetic.sqlite3'))
    gov.set_policy({'agent_enabled': True, 'cloud_enabled': True, 'audio_cloud_enabled': True})
    return gov


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 5))


def multipart_fields(content_type, content):
    message = BytesParser(policy=email_policy.default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + content)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        value = part.get_payload(decode=True)
        # Do not include WAV bytes in assertion diffs/logs.
        fields.setdefault(name, []).append(len(value) if name == 'file' else value.decode('utf-8'))
    return fields


def response(seconds=1, **extra):
    return {'text': '실내 온도 알려줘', 'usage': {'type': 'duration', 'seconds': seconds},
            'languages': [{'code': 'ko'}]} | extra


def test_default_model_does_not_enable_or_migrate_stored_policy(tmp_path, monkeypatch):
    fresh = BudgetPolicy()
    assert fresh.stt_model == 'gpt-transcribe'
    assert not any((fresh.agent_enabled, fresh.cloud_enabled, fresh.audio_cloud_enabled))
    assert policy_from_env().stt_model == 'gpt-transcribe'
    store = Store(tmp_path / 'saved.sqlite3')
    previous = BudgetGovernor(store)
    previous.set_policy({'stt_model': 'gpt-4o-mini-transcribe', 'monthly_cap_usd': 37.0})
    monkeypatch.setenv('HUB_OPENAI_STT_MODEL', 'gpt-transcribe')
    restarted = BudgetGovernor(store)
    assert restarted.policy().stt_model == 'gpt-4o-mini-transcribe'
    restarted.set_policy({'stt_model': 'gpt-transcribe'})
    assert restarted.policy().monthly_cap_usd == 37.0
    assert not restarted.policy().audio_cloud_enabled


def test_duration_cost_reservation_and_legacy_accounting_are_distinct(governor):
    assert calculate_cost_microdollars('gpt-transcribe', audio_seconds=60) == 4500
    assert calculate_cost_microdollars('gpt-transcribe', audio_seconds=6) == 450
    assert calculate_cost_microdollars('gpt-transcribe', audio_seconds=0.25) == 19
    with pytest.raises(ValueError):
        calculate_cost_microdollars('gpt-transcribe', prompt_tokens=100, audio_seconds=6)
    with pytest.raises(ValueError):
        governor.record_usage('missing-duration', 'stt', 'gpt-transcribe')
    duration = governor.gate('stt', audio_seconds=1.01)
    assert duration.selected_model == 'gpt-transcribe'
    assert duration.estimated_call_microdollars == 150  # two whole seconds reserved
    assert governor.gate('stt', requested_model='whisper-1', audio_seconds=6).estimated_call_microdollars == 600
    assert governor.gate('stt', requested_model='gpt-4o-mini-transcribe', audio_seconds=6).estimated_call_microdollars == 30000


def test_new_model_stays_selected_at_soft_caps_and_hard_cap_still_blocks(governor):
    governor.set_policy({'monthly_cap_usd': 1.0})
    governor.record_usage('synthetic-spend', 'agent_fast', 'gpt-6.1-sol', prompt_tokens=460000)
    # This cost is over the hard cap because of long-context pricing.
    assert not governor.gate('stt', audio_seconds=1).allowed_cloud


@pytest.mark.parametrize('invalid', [True, -0.5, float('inf'), float('nan'), '1'])
def test_original_duration_is_validated_before_reservation_rounding(governor, invalid):
    with pytest.raises(ValueError):
        governor.gate('stt', audio_seconds=invalid)


def test_fractional_wav_duration_does_not_round_down_before_reservation(governor):
    async def provider(*args):
        return response(seconds=1.000375)
    result = run(OpenAIGateway(governor, api_key='synthetic-only', transport=provider).transcribe_audio(
        request_id='fractional-audio', audio_bytes=make_wav_bytes(1.000375)))
    assert result['verified_duration_seconds'] == 1.000375
    assert result['cost_microdollars'] == 76
    assert governor.list_reservations()[0]['reserved_microdollars'] == 150


@pytest.mark.parametrize('spent', [700000, 900000])
def test_no_automatic_downgrade_to_deprecated_stt(governor, spent):
    governor.set_policy({'monthly_cap_usd': 1.0})
    # The synthetic ledger is test-only, not a fabricated provider response.
    with governor.store.connect() as db:
        db.execute('INSERT INTO hub_ai_usage_ledger(id,request_id,billing_month,category,model,cost_microdollars,created_at) VALUES(?,?,?,?,?,?,?)',
                   ('spent', 'spent', governor.current_billing_month(), 'chat', 'local', spent, 'synthetic'))
    decision = governor.gate('stt', audio_seconds=1)
    assert decision.allowed_cloud and not decision.downgraded
    assert decision.selected_model == 'gpt-transcribe'


@pytest.mark.parametrize('model', ['gpt-transcribe', 'gpt-4o-mini-transcribe', 'gpt-4o-transcribe', 'whisper-1'])
def test_real_httpx_multipart_fields_are_model_specific(governor, model):
    requests = []

    async def provider(request):
        fields = multipart_fields(request.headers['content-type'], await request.aread())
        requests.append(fields)
        usage = ({'type': 'duration', 'seconds': 1}
                 if model in {'gpt-transcribe', 'whisper-1'} else
                 {'type': 'tokens', 'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110})
        return httpx.Response(200, json=response(usage=usage))

    gw = OpenAIGateway(governor, api_key='synthetic-only', transport=httpx.MockTransport(provider))
    result = run(gw.transcribe_audio(request_id='fields-' + model, audio_bytes=make_wav_bytes(),
                                   language='ko', requested_model=model, prompt_hint='한국어 개인 비서 녹음.',
                                   keywords=['할 일', '실내 온도', 'ADC']))
    fields = requests[0]
    assert fields['model'] == [model]
    assert fields['response_format'] == ['json']
    assert fields['file'] == [len(make_wav_bytes())]
    assert result['cost_known'] is True
    assert governor.list_reservations()[0]['status'] == 'settled'
    assert result['cost_microdollars'] == {'gpt-transcribe': 75, 'whisper-1': 100,
                                         'gpt-4o-mini-transcribe': 175, 'gpt-4o-transcribe': 350}[model]
    if model == 'gpt-transcribe':
        assert fields['languages[]'] == ['ko']
        assert fields['keywords[]'] == ['할 일', '실내 온도', 'ADC']
        assert 'language' not in fields
        assert result['cost_microdollars'] == 75
    else:
        assert fields['language'] == ['ko']
        assert 'languages[]' not in fields and 'keywords[]' not in fields
        assert '할 일, 실내 온도, ADC' in fields['prompt'][0]
    assert result['detected_languages'] == ['ko']


def test_production_urllib_multipart_repeats_keywords_and_omits_auto_language(governor, monkeypatch):
    requests = []

    class Opener:
        def open(self, req, timeout):
            requests.append(multipart_fields(req.get_header('Content-type'), req.data))
            return io.BytesIO(json.dumps(response()).encode())

    monkeypatch.setattr('app.openai_gateway.urllib.request.build_opener', lambda *args: Opener())
    gw = OpenAIGateway(governor, api_key='synthetic-only')
    run(gw.transcribe_audio(request_id='urllib-auto', audio_bytes=make_wav_bytes(),
                           language='auto', keywords=['메모', '타이머']))
    run(gw.transcribe_audio(request_id='urllib-ko', audio_bytes=make_wav_bytes(), language='ko'))
    assert requests[0]['keywords[]'] == ['메모', '타이머']
    assert not {'language', 'languages[]'} & requests[0].keys()
    assert requests[1]['languages[]'] == ['ko']
    assert 'language' not in requests[1] and 'keywords[]' not in requests[1]


@pytest.mark.parametrize('keywords', ['not-a-list', [None], [''], ['word\nword'], ['word\r'],
                                      ['<bad>'], ['tab\t'], ['x' * 81], ['x'] * 65,
                                      ['가' * 79 + str(i) for i in range(20)]])
def test_invalid_keywords_rejected_before_reservation_or_network(governor, keywords):
    async def forbidden(*args):
        pytest.fail('invalid hint reached provider')
    gw = OpenAIGateway(governor, api_key='synthetic-only', transport=forbidden)
    with pytest.raises(OpenAIGatewayError) as caught:
        run(gw.transcribe_audio(request_id='invalid-hint', audio_bytes=make_wav_bytes(), keywords=keywords))
    assert caught.value.code == 'invalid_keywords'
    assert governor.list_reservations() == []


def test_keyword_normalization_and_request_id_bind_hints(governor):
    assert normalize_transcription_keywords([' ADC ', 'adc', '실내 온도']) == ['ADC', '실내 온도']
    calls = []
    async def provider(*args):
        calls.append(1)
        return response()
    gw = OpenAIGateway(governor, api_key='synthetic-only', transport=provider)
    run(gw.transcribe_audio(request_id='one-audio', audio_bytes=make_wav_bytes(), keywords=['타이머']))
    with pytest.raises(OpenAIGatewayError) as replay:
        run(gw.transcribe_audio(request_id='one-audio', audio_bytes=make_wav_bytes(), keywords=['타이머']))
    assert replay.value.code == 'request_already_dispatched'
    with pytest.raises(OpenAIGatewayError) as conflict:
        run(gw.transcribe_audio(request_id='one-audio', audio_bytes=make_wav_bytes(), keywords=['메모']))
    assert conflict.value.code == 'request_conflict'
    assert calls == [1]


@pytest.mark.parametrize('usage', [None, {}, {'type': 'duration'}, {'type': 'duration', 'seconds': 0},
                                   {'type': 'duration', 'seconds': True},
                                   {'type': 'tokens', 'input_tokens': 100, 'output_tokens': 10}])
def test_missing_or_wrong_billing_kind_keeps_unknown_reservation(governor, usage):
    async def provider(*args):
        return response(usage=usage)
    gw = OpenAIGateway(governor, api_key='synthetic-only', transport=provider)
    result = run(gw.transcribe_audio(request_id='unknown-duration', audio_bytes=make_wav_bytes()))
    assert result['cost_known'] is False and result['cost_usd'] is None
    assert governor.summary()['call_count'] == 0
    assert governor.summary()['unknown_microdollars'] == 75
    assert governor.list_reservations()[0]['status'] == 'unknown'


def test_provider_duration_not_local_wav_estimate_settles_paid_failure(governor):
    async def provider(*args):
        raise OpenAIGatewayError('Synthetic failure', 'stt_http_error', 500,
                                 response_body=response(seconds=1.25))
    gw = OpenAIGateway(governor, api_key='synthetic-only', transport=provider)
    with pytest.raises(OpenAIGatewayError):
        run(gw.transcribe_audio(request_id='paid-failure', audio_bytes=make_wav_bytes(2)))
    assert governor.list_reservations()[0]['reserved_microdollars'] == 150
    assert governor.list_reservations()[0]['status'] == 'settled'
    assert governor.summary()['spent_microdollars'] == 94


@pytest.mark.parametrize('languages, expected', [([], []), (None, None), ([{'code': 'ko'}], ['ko']),
                                               ([{'code': 'unknown'}], None)])
def test_detected_languages_not_invented_from_hint(governor, languages, expected):
    async def provider(*args):
        return response(languages=languages)
    result = run(OpenAIGateway(governor, api_key='synthetic-only', transport=provider).transcribe_audio(
        request_id='language-observation', audio_bytes=make_wav_bytes(), language='ko'))
    assert result['detected_languages'] == expected


def test_agent_registers_room_hub_hints_without_sending_memo_or_food_content(tmp_path):
    from app.food import FoodCreate
    from app.life import NoteCreate
    requests = []

    async def provider(request):
        fields = multipart_fields(request.headers['content-type'], await request.aread())
        requests.append(fields)
        return httpx.Response(200, json=response())

    _, life, _, _, agent, _ = build_test_harness(tmp_path, transport=httpx.MockTransport(provider),
                                               api_key='synthetic-only')
    for shared in (False, True):
        life.create_note(NoteCreate(request_id='memo-' + str(shared), title='NEVER_SEND_TITLE_' + str(shared),
                                    body='NEVER_SEND_BODY_' + str(shared), shared=shared))
    agent.food.create(FoodCreate(request_id='synthetic-food-hint', name='NEVER_SEND_FOOD_NAME',
                                 quantity=2, expiry_date='2042-06-27'))
    result = run(agent.transcribe_audio(request_id='agent-hint-integration', audio_bytes=make_wav_bytes()))
    assert result['model'] == 'gpt-transcribe'
    assert result['cost_known'] is True
    assert requests[0]['keywords[]'] == room_hub_transcription_hints()['keywords']
    assert {'소비기한', '식품', '개수'} <= set(requests[0]['keywords[]'])
    assert room_hub_transcription_hints()['prompt'] in requests[0]['prompt'][0]
    assert 'NEVER_SEND_' not in json.dumps(requests[0], ensure_ascii=False)
    assert '2042-06-27' not in json.dumps(requests[0], ensure_ascii=False)
    assert agent.list_runs()['total'] == 0  # transcribing alone never executes a command
