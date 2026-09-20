"""Whisper accuracy profiles, diagnostics and same-audio comparison.
No microphone, LLM, action or model/thread setting writes.
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'deploy/termux'))
from app.speech import SpeechConfig, WhisperRunner
from app.stt_accuracy import AccuracyConfig, task_hint, make_hint, PROFILES, PATCH_ID
from speech_model import atomic_write, data_json, sha256, port_closed, exclusive_lock, char_error_rate, reject_symlinks

BASE_SHA = '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe'


class ReadStore:
    """No table creation or hidden mutation when opening production DB."""
    def __init__(self, path: Path):
        self.path = path.resolve()
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db
    def get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None


def current(root: Path):
    config_path = root / 'data/speech-config.json'
    reject_symlinks(config_path)
    cfg = SpeechConfig.read(config_path)
    if not cfg.enabled or Path(cfg.model).name != 'ggml-base.bin' or cfg.language != 'ko' or cfg.threads != 6:
        raise ValueError('This test preserves multilingual base / ko / 6T. Current configuration is different.')
    ready, reason = cfg.readiness()
    if not ready:
        raise ValueError(reason)
    return cfg


def status(root: Path, verify: bool):
    cfg = current(root)
    policy = AccuracyConfig.read(root / 'data/stt-accuracy.json')
    if verify and sha256(Path(cfg.model)) != BASE_SHA:
        raise ValueError('Multilingual base SHA256 mismatch')
    return {'patch_id': PATCH_ID, 'app_version': (root/'VERSION').read_text().strip(),
            'model': Path(cfg.model).name, 'threads': cfg.threads, 'language': cfg.language,
            'model_sha256_verified': True if verify else None,
            'accuracy': policy.public(), 'microphone_changed': False, 'llm_changed': False}


def set_profile(root: Path, name: str, titles: str | None):
    current(root)
    p = root / 'data/stt-accuracy.json'
    with exclusive_lock(root/'data/.stt-accuracy-settings.lock'):
        old = AccuracyConfig.read(p)
        new = replace(old, profile=name, include_task_titles=(old.include_task_titles if titles is None else titles == 'on'))
        if p.exists():
            atomic_write(root / 'data' / f'stt-accuracy.before-{time.time_ns()}.json', p.read_bytes())
        atomic_write(p, data_json(asdict(new)))
    print(json.dumps(new.public(), ensure_ascii=False, indent=2))
    print('Only the accuracy profile changed; it applies when the next job starts. Base/6T unchanged.')


def engine_capabilities(cfg):
    result = subprocess.run([cfg.binary, '--help'], stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    text = (result.stdout + result.stderr).decode('utf-8', errors='replace')
    required = ['--beam-size', '--prompt', '--output-json', '--log-score']
    missing = [flag for flag in required if flag not in text]
    if missing:
        raise ValueError('Installed CLI is missing: ' + ', '.join(missing))


def selftest(root: Path):
    port_closed()
    cfg = current(root)
    if sha256(Path(cfg.model)) != BASE_SHA:
        raise ValueError('Multilingual base SHA256 mismatch')
    sample = root/'runtime/stt/whisper.cpp/samples/jfk.wav'
    if not sample.is_file():
        raise ValueError('Existing public sample jfk.wav is missing. No substitute or download was performed.')
    policy = AccuracyConfig(profile='balanced', include_task_titles=False)
    hint = {'text': 'fellow Americans, country, United States.', 'titles': [], 'source': 'public sample'}
    out = root/'data/stt-accuracy-tests/selftest.json'
    with exclusive_lock(root/'data/.v35-server.lock'):
        started = time.monotonic()
        atomic_write(out, data_json({'ok': False, 'state': 'running'}))
        try:
            engine_capabilities(cfg)
            text, duration, diag = asyncio.run(WhisperRunner().transcribe_detailed(
                sample, replace(cfg, language='en'), asyncio.Event(), root/'data/speech-tmp', policy, hint))
            ok = all(x in text.lower() for x in ['ask', 'country']) and diag['scores']['available']
            result = {'ok': ok, 'test': 'actual installed engine / public English sample, NOT Korean accuracy',
                      'text': text, 'duration': duration, 'elapsed': time.monotonic()-started, 'diagnostic': diag}
            atomic_write(out, data_json(result))
            if not ok:
                raise ValueError('Actual engine returned unexpected text or no token scores; inspect selftest.json')
        except BaseException as exc:
            atomic_write(out, data_json({'ok': False, 'error_type': type(exc).__name__, 'elapsed': time.monotonic()-started}))
            raise
    print('STT CHECK PASSED: actual installed engine; beam3 + prompt + score output. Korean accuracy is not tested here.')
    print('Saved:', out)


def latest_audio(store: ReadStore, data: Path, voice_id=None):
    with store.connect() as db:
        rows = db.execute("SELECT id,filename,kind FROM voice WHERE kind='audio' AND filename IS NOT NULL " +
                          ('AND id=? ' if voice_id else '') + 'ORDER BY created_at DESC,id DESC',
                          (voice_id,) if voice_id else ()).fetchall()
        active = db.execute("SELECT count(*) FROM speech_jobs WHERE status IN ('queued','running')").fetchone()[0]
    if active:
        raise ValueError('Finish/cancel queued speech jobs before comparison.')
    for row in rows:
        if Path(row['filename']).name != row['filename']:
            continue
        path = data/'audio'/row['filename']
        reject_symlinks(path)
        if path.is_file():
            return row['id'], path
    raise ValueError('No saved audio file found. Keep the recording in the inbox.')


def html_report(result):
    cells = []
    for item in result['runs']:
        d = item.get('diagnostic', {})
        cells.append('<tr><td>'+html.escape(item['profile'])+'</td><td>'+str(item.get('elapsed_s', '—'))+
                     '</td><td>'+html.escape(item.get('text', item.get('error_type', '')))+
                     '</td><td>'+html.escape(str(item.get('cer', '정답 없음')))+
                     '</td><td>'+html.escape(str(d.get('scores', {}).get('mean_token_p', '—')))+'</td></tr>')
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Room Hub STT 비교</title><style>body{font:16px system-ui;margin:32px;line-height:1.6}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #ccc;white-space:pre-wrap;text-align:left}pre{white-space:pre-wrap}</style>
<h1>같은 음성의 Whisper 디코딩 비교</h1><p>전사문 포함 개인 자료 · 토큰 확률은 정확도가 아닙니다. 시간은 서버 정지 상태에서 변환·모델 로딩·추론을 포함합니다. 마이크·업로드 시간 제외.</p>
<table><tr><th>프로필</th><th>시간(초)</th><th>전사문</th><th>CER</th><th>평균 token p</th></tr>'''+''.join(cells)+'''</table><h2>재현 조건·상세 기록</h2><pre>'''+html.escape(json.dumps(result, ensure_ascii=False, indent=2))+ '</pre></html>'


def compare(root: Path, profiles: list[str], runs: int, voice_id=None, reference_file=None):
    port_closed()
    cfg = current(root)
    if sha256(Path(cfg.model)) != BASE_SHA:
        raise ValueError('Multilingual base SHA256 mismatch')
    if not profiles or any(p not in PROFILES for p in profiles) or len(set(profiles)) != len(profiles):
        raise ValueError('Profiles must be unique: legacy,hint,balanced,careful')
    reference = None
    if reference_file:
        if reference_file.stat().st_size > 16000:
            raise ValueError('Reference file is too long')
        reference = reference_file.read_text('utf-8-sig').strip()
        char_error_rate(reference, '')
    store = ReadStore(root/'data/room-hub.sqlite3')
    before = (root/'data/speech-config.json').read_bytes()
    policy_before = (root/'data/stt-accuracy.json').read_bytes() if (root/'data/stt-accuracy.json').exists() else None
    with exclusive_lock(root/'data/.v35-server.lock'):
        engine_capabilities(cfg)
        vid, source = latest_audio(store, root/'data', voice_id)
        if source.stat().st_size > cfg.max_bytes:
            raise ValueError('Saved audio exceeds the configured upload size')
        base_policy = AccuracyConfig.read(root/'data/stt-accuracy.json')
        hint = task_hint(store, replace(base_policy, profile='balanced'))
        result = {'schema': 1, 'patch_id': PATCH_ID, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'source_voice_id': vid, 'source_sha256': sha256(source),
                  'model_sha256': BASE_SHA, 'engine_sha256': sha256(Path(cfg.binary)),
                  'model': 'ggml-base.bin', 'threads': cfg.threads, 'language': cfg.language,
                  'hint_snapshot': hint, 'reference': reference, 'runs': [],
                  'notes': ['No database/model/config edits.', 'No LLM or command parser used.',
                            'Same recording; cached model/temperature/order effects remain.',
                            'Scores are not calibrated correctness. No auto selection.',
                            'CER: NFC/casefold, whitespace and punctuation excluded; may exceed 1.']}
        for run in range(runs):
            for name in (profiles if run % 2 == 0 else list(reversed(profiles))):
                print(f'RUN {run+1}: {name}', flush=True)
                policy = replace(base_policy, profile=name)
                started = time.monotonic()
                try:
                    text, duration, diag = asyncio.run(WhisperRunner().transcribe_detailed(
                        source, cfg, asyncio.Event(), root/'data/speech-tmp', policy,
                        hint if name != 'legacy' else {'text': '', 'titles': []}))
                    elapsed = time.monotonic()-started
                    item = {'profile': name, 'run': run+1, 'text': text, 'audio_seconds': duration,
                            'elapsed_s': round(elapsed,4), 'rtf': elapsed/duration, 'diagnostic': diag}
                    if reference is not None:
                        item['cer'] = char_error_rate(reference, text)
                    result['runs'].append(item)
                except Exception as exc:
                    result['runs'].append({'profile': name, 'run': run+1, 'error_type': type(exc).__name__,
                                           'elapsed_s': round(time.monotonic()-started,4)})
        if before != (root/'data/speech-config.json').read_bytes():
            raise ValueError('Speech config changed externally during comparison')
        after = (root/'data/stt-accuracy.json').read_bytes() if (root/'data/stt-accuracy.json').exists() else None
        if after != policy_before:
            raise ValueError('Accuracy profile changed externally during comparison')
        folder = root/'data/stt-accuracy-tests'/('compare-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+str(time.time_ns()))
        folder.mkdir(parents=True, mode=0o700)
        atomic_write(folder/'report.json', data_json(result))
        atomic_write(folder/'report.html', html_report(result).encode())
        target = root/'data/stt-accuracy-tests/latest-compare.zip'
        reject_symlinks(target)
        # Private records only; never put this directory in a source archive.
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(folder/'report.json', 'report.json'); z.write(folder/'report.html', 'report.html')
        atomic_write(target, buffer.getvalue())
    print('SAVED:', target)
    print('No inbox text/config was overwritten. Start Room Hub again with sv up.')
    if any('error_type' in x for x in result['runs']):
        raise ValueError('Some comparison runs failed; see the report. No winner was selected.')


def recent(root: Path, count: int, export: bool):
    store = ReadStore(root/'data/room-hub.sqlite3')
    with store.connect() as db:
        rows = db.execute('''SELECT d.job_id,d.attempt,d.report,d.created_at
            FROM speech_diagnostics d ORDER BY d.created_at DESC LIMIT ?''', (count,)).fetchall()
    result = [{'job_id': r['job_id'], 'attempt': r['attempt'], 'created_at': r['created_at'],
               'diagnostic': json.loads(r['report'])} for r in rows]
    if export:
        dest = root/'data/stt-accuracy-tests/latest-diagnostics.json'
        atomic_write(dest, data_json(result)); print('SAVED:', dest)
    else:
        # Never prints transcripts/hints unless explicitly exported to private file.
        print(json.dumps([{'job_id': r['job_id'], 'profile': r['diagnostic']['profile'],
                          'beam': r['diagnostic']['beam_size'], 'threads': r['diagnostic']['threads'],
                          'seconds': r['diagnostic']['runner_total_seconds']} for r in result], indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    s=sub.add_parser('status'); s.add_argument('--verify', action='store_true')
    s=sub.add_parser('profile'); s.add_argument('name', choices=PROFILES); s.add_argument('--task-titles', choices=['on','off'])
    sub.add_parser('selftest')
    s=sub.add_parser('compare'); g=s.add_mutually_exclusive_group(required=True); g.add_argument('--latest', action='store_true'); g.add_argument('--voice-id')
    s.add_argument('--profiles', default='legacy,hint,balanced'); s.add_argument('--runs', type=int, choices=[1,2,3], default=1)
    s.add_argument('--reference-file', type=Path)
    s=sub.add_parser('recent'); s.add_argument('--count', type=int, choices=range(1,21), default=5); s.add_argument('--export', action='store_true')
    a=p.parse_args(argv)
    try:
        if a.command=='status': print(json.dumps(status(ROOT,a.verify),ensure_ascii=False,indent=2))
        elif a.command=='profile': set_profile(ROOT,a.name,a.task_titles)
        elif a.command=='selftest': selftest(ROOT)
        elif a.command=='compare': compare(ROOT,a.profiles.split(','),a.runs,a.voice_id,a.reference_file)
        elif a.command=='recent': recent(ROOT,a.count,a.export)
        return 0
    except KeyboardInterrupt:
        print('STOP: test cancelled; server was not started automatically.', file=sys.stderr); return 130
    except Exception as exc:
        print(f'STOP: {exc}', file=sys.stderr); return 1

if __name__=='__main__':
    raise SystemExit(main())
