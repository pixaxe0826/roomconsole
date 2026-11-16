"""Synthetic audio/manifest and fake provider only; not real STT accuracy."""
import asyncio
import ast
import io
import json
from pathlib import Path
import unicodedata
import wave

import httpx
import pytest

from app.openai_gateway import OpenAIGateway
from benchmarks.stt_metrics import normalize_for_cer, text_errors
from scripts import benchmark_openai_stt as benchmark


def wav_bytes(seconds=.5):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * int(seconds * 16000))
    return buffer.getvalue()


@pytest.fixture
def external_cases(tmp_path):
    folder = tmp_path / 'external-stt'
    folder.mkdir()
    (folder / 'sample.wav').write_bytes(wav_bytes())
    manifest = folder / 'cases.jsonl'
    manifest.write_text(json.dumps({'id': 'synthetic-a', 'audio': 'sample.wav',
                                   'reference': '서랍 정리', 'language': 'ko'},
                                  ensure_ascii=False) + '\n', encoding='utf-8')
    return manifest


def run(external_cases, tmp_path, handler, **overrides):
    cases, digest, _ = benchmark.load_cases(external_cases, 50)
    factory = lambda governor, **kw: OpenAIGateway(governor, api_key='synthetic-not-real',
                                                   transport=httpx.MockTransport(handler), **kw)
    kwargs = dict(models=list(benchmark.MODELS), hint_modes=['none', 'roomhub'],
                  max_cost_usd=1.0, repeats=1, timeout=5,
                  manifest_sha256=digest, audio_origin='synthetic', gateway_factory=factory)
    kwargs.update(overrides)
    return asyncio.run(benchmark.run_cases(cases, tmp_path / 'results', **kwargs))


def test_preflight_requires_key_opt_in_and_manifest_without_reading(monkeypatch, capsys, tmp_path):
    for name in ('OPENAI_API_KEY', 'HUB_OPENAI_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(benchmark, 'load_cases', lambda *a: pytest.fail('Must not read input before opt-in/key'))
    assert benchmark.main([]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report == {'status': 'NOT_RUN', 'reasons': [
        'live_not_authorized', 'missing_api_key', 'missing_external_audio_manifest'],
        'api_requests': 0, 'accuracy_measured': False}
    assert benchmark.main(['--live', '--manifest', str(tmp_path / 'absent.jsonl')]) == 2
    assert 'explicit_test_budget_required' in capsys.readouterr().out


def test_metrics_match_existing_v35_cer_without_importing_control_code():
    # Extract only two already-existing pure function ASTs. Do not import
    # Linux fcntl/model switching helpers on Windows or execute module setup.
    source = (benchmark.ROOT / 'deploy/termux/speech_model.py').read_text('utf-8')
    functions = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name in {'normalize_for_cer', 'char_error_rate'}]
    scope = {'unicodedata': unicodedata, 'Stop': ValueError}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<existing-pure-cer>', 'exec'), scope)
    pairs = [('서랍 정리', '서랍,정리.'), ('오늘 할 일', '오늘 할일'),
             ('ABC 12', 'abc 13'), ('가나다', ''), ('한글', '한글을 더 말했어')]
    for reference, hypothesis in pairs:
        existing = scope['char_error_rate'](reference, hypothesis)
        current = text_errors(reference, hypothesis)
        assert current['cer'] == existing['cer']
        assert current['character_edits'] == existing['edits']
        assert current['reference_characters'] == existing['reference_characters']
    assert normalize_for_cer('가 ABC!') == '가abc'
    assert text_errors('오늘 할 일', '오늘 할일')['wer'] == pytest.approx(2 / 3)
    with pytest.raises(ValueError):
        text_errors('... ', '무음')


@pytest.mark.parametrize('change', [
    {'audio': '../outside.wav'}, {'audio': 'wrong.mp3'}, {'language': 'Korean'},
    {'reference': ''}, {'prompt': 'leaked gold'}, {'id': '../escape'},
])
def test_manifest_rejects_invalid_or_hint_fields(external_cases, change):
    row = json.loads(external_cases.read_text('utf-8'))
    row.update(change)
    external_cases.write_text(json.dumps(row), encoding='utf-8')
    with pytest.raises((ValueError, OSError)):
        benchmark.load_cases(external_cases, 10)


def test_manifest_cannot_live_in_source_tree():
    with pytest.raises(ValueError, match='outside'):
        benchmark.load_cases(benchmark.ROOT / 'cases.jsonl', 10)


def test_four_variants_same_audio_no_gold_leaks_and_private_metrics(external_cases, tmp_path):
    calls = []
    async def handler(request):
        body = (await request.aread()).decode('utf-8', errors='replace')
        calls.append(body)
        assert '서랍 정리' not in body  # Human-checked gold never enters context.
        model = 'gpt-transcribe' if '\r\n\r\ngpt-transcribe\r\n' in body else 'gpt-4o-mini-transcribe'
        usage = {'type': 'duration', 'seconds': .5} if model == 'gpt-transcribe' else {
            'input_tokens': 20, 'output_tokens': 5}
        return httpx.Response(200, json={'text': '서랍 정리', 'usage': usage})
    report = run(external_cases, tmp_path, handler)
    assert report['status'] == 'COMPLETED'
    assert report['measurement'] == 'injected_test_provider_not_accuracy'
    assert len(calls) == len(report['rows']) == 4
    assert len({row['audio_sha256'] for row in report['rows']}) == 1
    assert all(row['scores']['cer'] == 0 for row in report['rows'])
    assert all(row['actual_model'] == row['requested_model'] for row in report['rows'])
    assert '서랍 정리' not in (tmp_path / 'results/report.json').read_text('utf-8')
    assert 'synthetic-not-real' not in (tmp_path / 'results/report.json').read_text('utf-8')
    assert all(s['attempted'] == 1 and s['unevaluated'] == 0 for s in report['summary'])
    assert 'name="keywords[]"' not in calls[0]
    assert 'name="keywords[]"' in calls[3]
    assert report['budget']['unknown_microdollars'] == 0


def test_unknown_usage_stops_without_retry_and_retains_reservation(external_cases, tmp_path):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={'text': '서랍 정리'})
    report = run(external_cases, tmp_path, handler)
    assert count == 1
    assert report['status'] == 'PARTIAL'
    assert report['stop_reason'] == 'unknown_cost_no_retry'
    assert report['budget']['unknown_microdollars'] > 0
    assert report['summary'][0]['total_cost_microdollars'] is None
    assert report['summary'][1]['cer'] is None
    assert report['summary'][1]['unevaluated'] == 1


def test_provider_failure_stops_and_excludes_failure_from_accuracy(external_cases, tmp_path):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(403, json={'error': {'message': 'redacted-provider-detail'}})
    report = run(external_cases, tmp_path, handler)
    assert count == 1
    assert report['rows'][0]['status'] == 'FAILED'
    assert all(item['cer'] is None for item in report['summary'])
    assert report['summary'][0]['failed'] == 1
    assert 'redacted-provider-detail' not in json.dumps(report)


def test_paid_http_failure_keeps_known_provider_cost(external_cases, tmp_path):
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={'error': {'message': 'private-error-body'},
                                        'usage': {'type': 'duration', 'seconds': .5}})
    report = run(external_cases, tmp_path, handler, models=['gpt-transcribe'], hint_modes=['roomhub'])
    assert calls == 1
    assert report['status'] == 'PARTIAL'
    assert report['rows'][0]['status'] == 'FAILED'
    assert report['rows'][0]['cost_microdollars'] > 0
    assert report['summary'][0]['unknown_cost_calls'] == 0
    assert report['summary'][0]['total_cost_microdollars'] == report['budget']['spent_microdollars']
    assert report['summary'][0]['cer'] is None
    assert 'private-error-body' not in json.dumps(report)


def test_no_overwrite_or_repeated_paid_run(external_cases, tmp_path):
    (tmp_path / 'results').mkdir()
    async def handler(request):
        pytest.fail('Existing run directory must not cause a new request')
    with pytest.raises(FileExistsError):
        run(external_cases, tmp_path, handler)


def test_budget_cap_blocks_before_network_and_ignores_production_env(external_cases, tmp_path, monkeypatch):
    protected = tmp_path / 'production-sentinel'
    monkeypatch.setenv('HUB_DATA_DIR', str(protected))
    async def handler(request):
        pytest.fail('No provider call is allowed after the isolated cap')
    def factory(governor, **kw):
        assert not governor.policy().auto_downgrade
        assert not governor.policy().fallback_to_local
        governor.record_usage('synthetic-budget-exhaustion', 'agent_fast', 'gpt-4o', prompt_tokens=400_000)
        return OpenAIGateway(governor, api_key='synthetic-not-real',
                             transport=httpx.MockTransport(handler), **kw)
    report = run(external_cases, tmp_path, handler, gateway_factory=factory)
    assert report['status'] == 'PARTIAL'
    assert report['budget']['state'] == 'HARD_CAP_REACHED'
    assert report['rows'][0]['status'] == 'FAILED'
    assert not protected.exists()


def test_no_production_db_or_agent_imports():
    source = Path(benchmark.__file__).read_text('utf-8')
    assert 'from app.main' not in source
    assert 'from app.personal_agent' not in source
    assert 'HUB_DATA_DIR' not in source
