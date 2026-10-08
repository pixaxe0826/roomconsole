"""Mock HTTP only. This is not an ASR accuracy or GPU performance measurement."""
import asyncio
from dataclasses import asdict, replace
from email.parser import BytesParser
from email.policy import default
import json
from pathlib import Path
import sys

import httpx
import pytest

from app.speech import SpeechConfig, WhisperRunner
from app.remote_speech import RemoteWhisperRunner, RemoteSpeechError, validate_endpoint, MAX_RESPONSE_BYTES
from app.stt_accuracy import AccuracyConfig, make_hint, serialize_report


@pytest.fixture
def config():
    return SpeechConfig(enabled=True, backend='remote_http', remote_url='http://127.0.0.1:8178/inference',
                        remote_model='medium', remote_device='cuda', ffmpeg=sys.executable)


@pytest.fixture
def fake_wav(monkeypatch):
    async def prepare(source, cfg, cancel, dest):
        dest.write_bytes(b'RIFF' + b'synthetic-pcm'*20)
        return 1.25
    monkeypatch.setattr('app.remote_speech.prepare_wav', prepare)


@pytest.mark.parametrize('endpoint', [
    'https://127.0.0.1:8178/inference', 'http://localhost:8178/inference',
    'http://192.0.2.10:8178/inference', 'http://127.0.0.1/inference',
    'http://127.0.0.1:8088/inference', 'http://127.0.0.1:8443/inference',
    'http://127.0.0.1:8178/load', 'http://127.0.0.1:8178/inference?url=bad',
    'http://127.0.0.1:8178/inference#frag', 'http://secret@127.0.0.1:8178/inference',
    'http://127.0.0.1:8178/inference\n', 'http://[::ffff:127.0.0.1]:8178/inference',
    'http://127.0.0.1:8178/inference/', 'http://127.0.0.1:8178/%69nference',
])
def test_endpoint_rejects_unsafe_targets(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:8178/inference', 'http://[::1]:18178/inference'])
def test_endpoint_accepts_literal_loopback(endpoint):
    assert validate_endpoint(endpoint) == endpoint


def test_old_configuration_defaults_and_remote_no_local_model_requirement(config, tmp_path):
    p = tmp_path / 'speech-config.json'
    p.write_text(json.dumps({'enabled': False, 'ffmpeg': sys.executable}))
    assert SpeechConfig.read(p).backend == 'local_cli'
    p.write_text(json.dumps(asdict(config)))
    cfg = SpeechConfig.read(p)
    assert cfg.readiness() == (True, '')
    assert not cfg.binary and not cfg.model
    assert replace(cfg, backend='local_cli', remote_url='').readiness()[0] is False


@pytest.mark.parametrize('field,value', [('backend','other'), ('remote_device','invented'), ('remote_model','bad\nlabel')])
def test_bad_remote_config_fails_closed(config, tmp_path, field, value):
    p = tmp_path / 'speech-config.json'; data = asdict(config); data[field] = value
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        SpeechConfig.read(p)


def multipart(req, content):
    message = BytesParser(policy=default).parsebytes(('Content-Type: '+req.headers['Content-Type']+'\r\n\r\n').encode()+content)
    return {p.get_param('name', header='content-disposition'): p.get_payload(decode=True) for p in message.iter_parts()}


def run_transcribe(runner, cfg, tmp_path, policy=None, hint=None):
    return asyncio.run(runner.transcribe_detailed(tmp_path/'private-original-name.bin', cfg, asyncio.Event(),
                       tmp_path/'tmp', policy or AccuracyConfig(profile='careful'), hint or {'text':'', 'titles':[]}))


@pytest.mark.parametrize('profile,beam', [('legacy',1), ('hint',1), ('balanced',3), ('careful',5)])
def test_form_preserves_policy_hint_and_truthful_metadata(config, tmp_path, fake_wav, profile, beam):
    calls=[]
    async def handler(req):
        fields=multipart(req, await req.aread());calls.append(fields)
        assert req.method == 'POST' and req.url.path == '/inference'
        assert fields['file'].startswith(b'RIFF')
        assert fields['beam_size'] == str(beam).encode()
        assert fields['best_of'] == b'1'
        assert fields['language'] == b'ko'
        assert fields['translate'] == b'false'
        assert fields['no_language_probabilities'] == b'true'
        assert fields['no_timestamps'] == b'false'
        assert fields['response_format'] == b'verbose_json'
        assert fields['prompt'].decode() == prompt
        assert 'model' not in fields and 'threads' not in fields
        return httpx.Response(200, json={'text':' 합성 전사 시험입니다.\n', 'language':'korean',
              'segments':[{'text':' 합성 전사 시험입니다.', 'start':0, 'end':1.25}]})
    policy=AccuracyConfig(profile=profile);prompt,titles=make_hint(policy, ['별빛 기록'])
    runner=RemoteWhisperRunner(httpx.MockTransport(handler))
    text,duration,report=run_transcribe(runner, config, tmp_path, policy, {'text':prompt,'titles':titles})
    assert len(calls) == 1 and text == '합성 전사 시험입니다.' and duration == 1.25
    assert report['backend'] == 'remote_http'
    assert report['model_name'] == 'medium' and report['model_identity_source'] == 'operator_config_not_verified'
    assert report['reported_model'] is None and report['device'] is None and report['cpu_only'] is None
    assert report['configured_device'] == 'cuda' and report['threads'] is None
    assert report['whisper_seconds_including_load'] is None and report['remote_request_seconds'] >= 0
    assert report['segments']['segments'][0]['offsets_ms']['to'] == 1250
    assert not report['scores']['available'] and not report['automatic_second_pass']
    assert json.loads(serialize_report(report))['backend'] == 'remote_http'
    assert list((tmp_path/'tmp').iterdir()) == []
    assert not (tmp_path/'private-original-name.bin').exists()


@pytest.mark.parametrize('status,body,ctype,code', [
    (302, b'private-upstream-detail', 'text/plain', 'http_error'),
    (401, b'private-upstream-detail', 'text/plain', 'http_error'),
    (503, b'private-upstream-detail', 'text/plain', 'loading'),
    (200, b'{"error":"private-upstream-detail"}', 'application/json', 'invalid_response'),
    (200, b'not json', 'application/json', 'invalid_response'),
    (200, b'{}', 'application/json', 'invalid_transcript'),
    (200, b'{"text":null}', 'application/json', 'invalid_transcript'),
    (200, b'{"text":" "}', 'application/json', 'invalid_transcript'),
    (200, b'{"text":"a","text":"b"}', 'application/json', 'invalid_response'),
    (200, b'{"text":"a","value":NaN}', 'application/json', 'invalid_response'),
    (200, b'html', 'text/html', 'invalid_response'),
    (200, b'a'*(MAX_RESPONSE_BYTES+1), 'application/json', 'response_too_large'),
])
def test_remote_failure_no_retry_no_raw_error(config, tmp_path, fake_wav, status, body, ctype, code):
    calls=[]
    def handler(req):
        calls.append(req.url)
        return httpx.Response(status, content=body, headers={'Content-Type':ctype,'Location':'http://192.0.2.1/private'})
    runner=RemoteWhisperRunner(httpx.MockTransport(handler))
    with pytest.raises(RemoteSpeechError) as caught:
        run_transcribe(runner,config,tmp_path)
    assert caught.value.code == code and 'private-upstream-detail' not in str(caught.value)
    assert len(calls) == 1 and not list((tmp_path/'tmp').iterdir())


def test_offline_no_local_cli_fallback(config, tmp_path, fake_wav, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail('must not fall back to local Whisper')
    monkeypatch.setattr(WhisperRunner,'transcribe_detailed',forbidden)
    calls=[]
    def handler(req):
        calls.append(req)
        raise httpx.ConnectError('private socket details',request=req)
    with pytest.raises(RemoteSpeechError,match='PC') as caught:
        run_transcribe(RemoteWhisperRunner(httpx.MockTransport(handler)),config,tmp_path)
    assert caught.value.code=='connection_failed' and len(calls)==1


@pytest.mark.parametrize('cancelled', [False, True])
def test_timeout_or_cancel_joins_transport(config, tmp_path, fake_wav, cancelled):
    async def go():
        entered=asyncio.Event();closed=asyncio.Event();cancel=asyncio.Event()
        async def handler(req):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                closed.set()
        runner=RemoteWhisperRunner(httpx.MockTransport(handler))
        cfg=replace(config,timeout_seconds=.1)
        task=asyncio.create_task(runner.transcribe_detailed(tmp_path/'in',cfg,cancel,tmp_path/'tmp',AccuracyConfig(),{'text':''}))
        await entered.wait()
        if cancelled:
            cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RemoteSpeechError) as caught:
                await task
            assert caught.value.code=='timeout'
        assert closed.is_set()
        assert not list((tmp_path/'tmp').iterdir())
    asyncio.run(go())


def test_probe_health_only_and_separate_secret(config, monkeypatch):
    monkeypatch.setenv('HUB_ADMIN_TOKEN','admin-not-forwarded')
    monkeypatch.setenv('HUB_LLM_API_KEY','llm-not-forwarded')
    monkeypatch.setenv('HUB_SPEECH_API_KEY','speech-test-key')
    monkeypatch.setenv('HTTP_PROXY','http://192.0.2.1:9')
    calls=[]
    def handler(req):
        calls.append(req)
        assert req.method=='GET' and req.url.path=='/health'
        assert req.headers['Authorization']=='Bearer speech-test-key'
        assert not req.content
        return httpx.Response(200,json={'status':'ok'})
    result=asyncio.run(RemoteWhisperRunner(httpx.MockTransport(handler)).probe(config))
    assert result['ok'] and len(calls)==1
    assert not any(secret in json.dumps(result) for secret in ['speech-test-key','llm-not-forwarded','admin-not-forwarded'])


def test_stream_deadline_covers_drip_response_and_closes_stream(config, tmp_path, fake_wav):
    async def go():
        closed=asyncio.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"text":"'
                await asyncio.sleep(10)
                yield b'ok"}'
            async def aclose(self):
                closed.set()
        runner=RemoteWhisperRunner(httpx.MockTransport(lambda r: httpx.Response(
            200,stream=SlowStream(),headers={'Content-Type':'application/json'})))
        with pytest.raises(RemoteSpeechError) as caught:
            await runner.transcribe_detailed(tmp_path/'in',replace(config,timeout_seconds=.05),
                asyncio.Event(),tmp_path/'tmp',AccuracyConfig(),{'text':''})
        assert caught.value.code=='timeout' and closed.is_set()
        assert not list((tmp_path/'tmp').iterdir())
    asyncio.run(go())


def test_invalid_unicode_rejected_and_huge_offsets_not_trusted(config, tmp_path, fake_wav):
    bad=RemoteWhisperRunner(httpx.MockTransport(lambda r: httpx.Response(200,
        content=b'{"text":"\\ud800"}',headers={'Content-Type':'application/json'})))
    with pytest.raises(RemoteSpeechError) as error:
        run_transcribe(bad,config,tmp_path)
    assert error.value.code=='invalid_transcript'
    good=RemoteWhisperRunner(httpx.MockTransport(lambda r: httpx.Response(200,json={
        'text':'합성 결과', 'segments':[{'text':'합성 결과','start':10**400,'end':10**401}]})))
    _,_,report=run_transcribe(good,config,tmp_path)
    assert 'offsets_ms' not in report['segments']['segments'][0]
