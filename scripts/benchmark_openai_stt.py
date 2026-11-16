"""Opt-in, same-audio OpenAI STT comparison. Never submits agent commands.

The old 250-case `python -m benchmarks` suite is text-only and independent.
This runner consumes an external JSONL manifest and already prepared PCM WAVs.
Neither references nor transcripts become vocabulary hints or source artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ai_profile import AIProfile
from app.openai_budget import BudgetGovernor, BudgetPolicy, PRICING_CATALOG_METADATA
from app.openai_gateway import (MAX_AUDIO_BYTES, OpenAIGateway,
                                OpenAIGatewayError, resolve_openai_api_key,
                                verify_audio_payload)
from app.store import Store
from benchmarks.paths import NAME, external_directory, no_links, owned_child, result_directory
from benchmarks.stt_metrics import percentile, text_errors

MODELS = ('gpt-4o-mini-transcribe', 'gpt-transcribe')
MAX_CASES = 500
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_TOTAL_AUDIO_BYTES = 256 * 1024 * 1024


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def preflight(manifest: Path | None, *, live: bool, has_key: bool) -> dict:
    """Do not read the manifest or audio before explicit opt-in and a key."""
    reasons = []
    if not live:
        reasons.append('live_not_authorized')
    if not has_key:
        reasons.append('missing_api_key')
    if manifest is None or not manifest.is_file():
        reasons.append('missing_external_audio_manifest')
    return {'status': 'NOT_RUN' if reasons else 'READY_FOR_VALIDATION',
            'reasons': reasons, 'api_requests': 0, 'accuracy_measured': False}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest field')
        result[key] = value
    return result


def read_audio(path: Path) -> tuple[bytes, float]:
    no_links(path)
    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError('Audio exceeds the provider gateway file limit')
    audio = path.read_bytes()
    verified = verify_audio_payload(audio, filename='voice.wav', mime_type='audio/wav')
    with wave.open(io.BytesIO(audio), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError('Use preprocessed mono PCM16 16000 Hz WAV files')
    return audio, verified['verified_duration_seconds']


def load_cases(manifest: Path, limit: int) -> tuple[list[dict], str, Path]:
    if not 1 <= limit <= MAX_CASES:
        raise ValueError('Case limit must be 1..500')
    root = external_directory(manifest.absolute().parent)
    manifest = owned_child(root, manifest.name)
    if manifest.suffix.lower() != '.jsonl' or manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Supply a bounded external JSONL manifest')
    source = manifest.read_bytes()
    rows = []
    seen = set()
    total_audio_bytes = 0
    for line in source.decode('utf-8-sig').splitlines():
        if not line.strip():
            continue
        row = json.loads(line, object_pairs_hook=_unique_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
        if not isinstance(row, dict) or set(row) - {'id', 'audio', 'reference', 'language'}:
            raise ValueError('Manifest fields are id, audio, reference, optional language only')
        if not isinstance(row.get('id'), str) or not NAME.fullmatch(row['id']) or row['id'] in seen:
            raise ValueError('Use unique bounded case identifiers')
        seen.add(row['id'])
        if len(seen) > MAX_CASES:
            raise ValueError('Manifest exceeds 500 cases; split it explicitly')
        if not isinstance(row.get('reference'), str) or len(row['reference']) > 16000:
            raise ValueError('A bounded human-checked reference is required')
        text_errors(row['reference'], '')
        language = row.get('language', 'ko')
        if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2}|auto', language):
            raise ValueError('Use a two-letter language code or auto')
        path = owned_child(root, row.get('audio'))
        if path.suffix.lower() != '.wav':
            raise ValueError('Only preprocessed WAV inputs are accepted')
        if len(rows) < limit:
            audio, seconds = read_audio(path)
            total_audio_bytes += len(audio)
            if total_audio_bytes > MAX_TOTAL_AUDIO_BYTES:
                raise ValueError('Selected audio exceeds the bounded batch size')
            rows.append({'id': row['id'], 'path': path, 'reference': row['reference'],
                         'language': language, 'audio_sha256': _sha(audio),
                         'audio_seconds': seconds,
                         'reference_sha256': _sha(row['reference'].encode('utf-8'))})
    if not rows:
        raise ValueError('No audio cases were selected')
    return rows, _sha(source), root


def aggregate(rows: list[dict], variants: list[tuple[str, str]], expected_per_variant: int) -> list[dict]:
    result = []
    for model, hints in variants:
        selected = [r for r in rows if (r['requested_model'], r['hints']) == (model, hints)]
        scored = [r for r in selected if r['status'] == 'SUCCEEDED']
        chars = sum(r['scores']['reference_characters'] for r in scored)
        words = sum(r['scores']['reference_words'] for r in scored)
        known_cost = sum(r['cost_microdollars'] for r in selected if r.get('cost_microdollars') is not None)
        unknown_cost = sum(r.get('cost_microdollars') is None for r in selected)
        times = [r['latency_seconds'] for r in scored]
        result.append({'model': model, 'hints': hints, 'planned': expected_per_variant,
                       'attempted': len(selected), 'scored': len(scored),
                       'failed': len(selected) - len(scored),
                       'unevaluated': expected_per_variant - len(selected),
                       'cer': sum(r['scores']['character_edits'] for r in scored) / chars if chars else None,
                       'wer': sum(r['scores']['word_edits'] for r in scored) / words if words else None,
                       'normalized_exact': sum(r['scores']['normalized_exact'] for r in scored),
                       'latency_p50_seconds': percentile(times, .50),
                       'latency_p95_seconds': percentile(times, .95),
                       'known_cost_microdollars': known_cost, 'unknown_cost_calls': unknown_cost,
                       'total_cost_microdollars': None if unknown_cost or len(selected) < expected_per_variant else known_cost})
    return result


def _save_report(path: Path, report: dict) -> None:
    # The new 0700 run directory is owned by this invocation, never reused.
    with path.open('w', encoding='utf-8') as file:
        os.chmod(path, 0o600)
        json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())


async def run_cases(cases: list[dict], output: Path, *, models: list[str],
                    hint_modes: list[str], max_cost_usd: float, repeats: int,
                    timeout: float, manifest_sha256: str, audio_origin: str,
                    gateway_factory=OpenAIGateway) -> dict:
    if not models or len(models) != len(set(models)) or any(m not in MODELS for m in models):
        raise ValueError('Select unique supported comparison models')
    if not hint_modes or len(hint_modes) != len(set(hint_modes)) or any(h not in {'none', 'roomhub'} for h in hint_modes):
        raise ValueError('Select none and/or roomhub hints')
    if not math.isfinite(max_cost_usd) or not 1 <= max_cost_usd <= 100:
        raise ValueError('Explicit test budget must be USD 1..100')
    if not 1 <= repeats <= 3 or not 5 <= timeout <= 120:
        raise ValueError('Repeats must be 1..3 and timeout 5..120 seconds')
    from app.transcription_hints import room_hub_transcription_hints
    hints = room_hub_transcription_hints()
    output = result_directory(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    no_links(output)
    store = Store(output / 'budget.sqlite3')
    governor = BudgetGovernor(store)
    governor.set_policy(BudgetPolicy(agent_enabled=True, cloud_enabled=True,
                                    audio_cloud_enabled=True, monthly_cap_usd=max_cost_usd,
                                    stt_model=models[0], auto_downgrade=False, fallback_to_local=False))
    gateway = gateway_factory(governor, profile=AIProfile('odyssey_openai'))
    created_at = datetime.now(timezone.utc).isoformat()
    variants = [(model, mode) for model in models for mode in hint_modes]
    report = {'schema': 1, 'status': 'RUNNING',
              'measurement': 'live_api_stt_only' if gateway_factory is OpenAIGateway else 'injected_test_provider_not_accuracy',
              'audio_origin': audio_origin, 'created_at': created_at,
              'manifest_sha256': manifest_sha256, 'cases': len(cases), 'repeats': repeats,
              'max_cost_usd': max_cost_usd, 'hint_version': hints['version'],
              'pricing': PRICING_CATALOG_METADATA, 'planned_requests': len(cases) * repeats * len(variants),
              'rows': [], 'summary': [], 'stop_reason': None,
              'notes': ['No agent, planner, production DB, command or device is used.',
                        'Only fixed Room Hub vocabulary is sent; gold/reference text is never a hint.',
                        'WAV preprocessing is outside timing; latency is gateway HTTP duration, not microphone-to-action.',
                        'Order rotates per case/repeat; network, provider load and warm-cache effects remain.',
                        'CER: NFC/casefold without whitespace/punctuation; WER: whitespace words, not Korean morphology.',
                        'Only successful transcripts are scored; failures and unattempted cases remain explicit.',
                        'No automatic retries. Unknown usage retains budget reservation and stops the run.',
                        'Ledger cost is application accounting, not the provider invoice.',
                        'Reports contain hashes and metrics, not audio, reference text or transcript text.']}
    report_file = output / 'report.json'
    def checkpoint():
        report['summary'] = aggregate(report['rows'], variants, len(cases) * repeats)
        budget = governor.summary(at_iso=created_at)
        report['budget'] = {key: value for key, value in budget.items()
                            if key in {'spent_microdollars', 'reserved_microdollars', 'unknown_microdollars',
                                       'spent_usd', 'reserved_usd', 'unknown_usd', 'committed_microdollars',
                                       'committed_usd', 'state'}}
        _save_report(report_file, report)
    checkpoint()
    try:
        for repeat in range(repeats):
            for index, case in enumerate(cases):
                audio, seconds = read_audio(case['path'])
                if _sha(audio) != case['audio_sha256']:
                    raise ValueError('Audio changed after validation; the batch is stopped')
                offset = (repeat + index) % len(variants)
                for model, mode in variants[offset:] + variants[:offset]:
                    request_id = 'stt-bench-' + uuid.uuid4().hex
                    row = {'request_id': request_id, 'case_id': case['id'], 'repeat': repeat + 1, 'requested_model': model,
                           'hints': mode, 'audio_sha256': case['audio_sha256'],
                           'reference_sha256': case['reference_sha256'], 'audio_seconds': seconds}
                    try:
                        response = await gateway.transcribe_audio(
                            request_id=request_id, audio_bytes=audio,
                            language=case['language'], requested_model=model, timeout=timeout,
                            prompt_hint=hints['prompt'] if mode == 'roomhub' else '',
                            keywords=hints['keywords'] if mode == 'roomhub' else None,
                            at_iso=created_at)
                        if response['model'] != model:
                            raise ValueError('Provider selection changed; do not compare under the wrong model')
                        row.update(status='SUCCEEDED', actual_model=response['model'],
                                   latency_seconds=response['latency_seconds'],
                                   scores=text_errors(case['reference'], response['text']),
                                   transcript_sha256=_sha(response['text'].encode('utf-8')),
                                   cost_microdollars=response['cost_microdollars'],
                                   cost_source=response['cost_source'])
                    except OpenAIGatewayError as exc:
                        # Safe structured code only; never log response bodies or keys.
                        row.update(status='FAILED', error_code=exc.code, http_status=exc.http_status,
                                   cost_microdollars=None)
                        # Failed HTTP/empty transcript may still have billable
                        # provider usage. Read only this request's isolated ledger.
                        with store.connect() as db:
                            accounted = db.execute(
                                'SELECT model,cost_microdollars,cost_source FROM hub_ai_usage_ledger WHERE request_id=?',
                                (request_id,)).fetchone()
                        if accounted is not None:
                            row.update(actual_model=accounted['model'],
                                       cost_microdollars=accounted['cost_microdollars'],
                                       cost_source=accounted['cost_source'])
                        report['stop_reason'] = 'provider_or_budget_error_no_retry'
                    report['rows'].append(row)
                    if row.get('cost_microdollars') is None:
                        report['stop_reason'] = report['stop_reason'] or 'unknown_cost_no_retry'
                    checkpoint()
                    if report['stop_reason']:
                        report['status'] = 'PARTIAL'
                        checkpoint()
                        return report
        report['status'] = 'COMPLETED'
    except BaseException as exc:
        report['status'] = 'INTERRUPTED' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'FAILED'
        report['stop_reason'] = 'local_' + type(exc).__name__
        checkpoint()
        raise
    checkpoint()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, help='External JSONL; never the text-only 250-case suite')
    parser.add_argument('--results-root', type=Path, default=ROOT / 'artifacts/benchmarks/stt')
    parser.add_argument('--name', default='stt-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    parser.add_argument('--live', action='store_true', help='Explicit consent to audio upload and paid requests')
    parser.add_argument('--max-cost-usd', type=float, help='Required for --live: separate test cap USD 1..100')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=45)
    parser.add_argument('--models', nargs='+', choices=MODELS, default=list(MODELS))
    parser.add_argument('--hints', choices=['none', 'roomhub', 'both'], default='both')
    parser.add_argument('--audio-origin', choices=['human', 'synthetic', 'mixed', 'unknown'], default='unknown')
    args = parser.parse_args(argv)
    check = preflight(args.manifest, live=args.live, has_key=bool(resolve_openai_api_key()))
    if args.live and args.max_cost_usd is None:
        check['status'] = 'NOT_RUN'
        check['reasons'].append('explicit_test_budget_required')
    if check['status'] == 'NOT_RUN':
        print(json.dumps(check, ensure_ascii=False))
        return 2
    try:
        if not NAME.fullmatch(args.name):
            raise ValueError('Use a simple unique run name')
        cases, manifest_sha, root = load_cases(args.manifest, args.limit)
        results = result_directory(args.results_root, root)
        results.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = asyncio.run(run_cases(cases, results / args.name, models=args.models,
            hint_modes=['none', 'roomhub'] if args.hints == 'both' else [args.hints],
            max_cost_usd=args.max_cost_usd, repeats=args.repeats, timeout=args.timeout,
            manifest_sha256=manifest_sha, audio_origin=args.audio_origin))
        print(json.dumps({'status': report['status'], 'gateway_attempts': len(report['rows']),
                          'summary': report['summary'], 'report': str(results / args.name / 'report.json')},
                         ensure_ascii=False, indent=2))
        return 0 if report['status'] == 'COMPLETED' else 3
    except (ValueError, OSError, KeyError, TypeError, wave.Error):
        # Input contents/absolute paths may be private; do not echo exception data.
        print(json.dumps({'status': 'FAILED', 'reason': 'invalid_input_or_local_io',
                          'note': 'Check the external manifest, WAV format, limits and unused result name.'}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'status': 'INTERRUPTED', 'reason': 'cancelled_no_retry'}))
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
