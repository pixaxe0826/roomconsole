"""Room Hub 0.1.3 model-only toolkit with multilingual small support (small-t8-1).

Reuses production SpeechConfig + WhisperRunner (same media conversion/CLI flags).
prepare: verified model download only; may run while Room Hub is serving.
switch/restore/compare: require stopped 8088 + exclusive V35 launcher lock.
No runtime CLI bypass of integrity checks. No DB writes, API keys, cloud ASR,
engine rebuild, privileged process kills, TLS changes or automatic task execution.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import sqlite3
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.speech import SpeechConfig, WhisperRunner, TranscriptionError

KIT_VERSION = '0.1.3-small-t8-1'
PORT = 8088

class Stop(RuntimeError):
    pass


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def data_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def reject_symlinks(path: Path) -> None:
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise Stop('Symlink path is not supported; inspect the target before proceeding.')


def atomic_write(path: Path, content: bytes) -> None:
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        # Directory fsync is unsupported on some Android-backed PRoot filesystems.
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


@contextlib.contextmanager
def exclusive_lock(path: Path):
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as file:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop('Another server/tool still owns the lock. Stop only Room Hub and retry.') from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def port_closed(port=PORT):
    with socket.socket() as sock:
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', port))
    if result == 0:
        raise Stop('Port 8088 is still open. Stop Room Hub before switch/restore/compare.')
    if result != errno.ECONNREFUSED:
        raise Stop('Cannot confirm the local port is closed; investigate before changing configuration.')


def normalize_for_cer(text: str) -> str:
    # Stable comparison metric: NFC, casefold, remove whitespace and punctuation.
    # Keep numeric/symbol differences. This is not an official language benchmark.
    return ''.join(ch for ch in unicodedata.normalize('NFC', text).casefold()
                   if not ch.isspace() and not unicodedata.category(ch).startswith('P'))


def char_error_rate(reference: str, hypothesis: str):
    left, right = normalize_for_cer(reference), normalize_for_cer(hypothesis)
    if not left:
        raise Stop('Reference text has no scorable characters.')
    if len(left) > 4000 or len(right) > 4000:
        raise Stop('Reference/transcript is too long for this short-utterance CER test.')
    prev = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            row.append(min(row[-1] + 1, prev[j] + 1, prev[j - 1] + (a != b)))
        prev = row
    return {'edits': prev[-1], 'reference_characters': len(left), 'cer': prev[-1] / len(left)}


class ModelLab:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.data = self.root / 'data'
        self.cfg_path = self.data / 'speech-config.json'
        self.work = self.data / 'speech-model-tests'
        self.models_dir = self.root / 'runtime/stt/models'
        self.catalog = json.loads((self.root / 'deploy/termux/speech-models.json').read_text('utf-8'))
        if (self.root / 'VERSION').read_text('utf-8').strip() not in {'0.1.3', '0.1.4', '0.1.5', '0.1.6'}:
            raise Stop('This model-only toolkit is for Room Hub 0.1.3/0.1.4/0.1.5/0.1.6. No app code was changed.')
        if os.environ.get('HUB_DATA_DIR') and Path(os.environ['HUB_DATA_DIR']).resolve() != self.data:
            raise Stop('Non-default HUB_DATA_DIR; do not operate on a different database/config.')

    def read_config(self):
        reject_symlinks(self.cfg_path)
        if not self.cfg_path.is_file():
            raise Stop('Existing working speech-config.json is required; do not reinstall the app.')
        cfg = SpeechConfig.read(self.cfg_path)
        if not cfg.enabled:
            raise Stop('Existing transcription must be enabled before this model-only experiment.')
        if cfg.language != 'ko':
            raise Stop('This Korean test expects language=ko; other settings were not changed.')
        if not 1 <= cfg.threads <= 8:
            raise Stop(f'Transcription threads must remain within 1..8; current config has threads={cfg.threads}.')
        for p in [Path(cfg.binary), Path(cfg.model)]:
            if not p.resolve().is_relative_to(self.root):
                raise Stop('A custom engine/model outside the bound Room Hub folder needs manual review.')
            reject_symlinks(p)
        ready, reason = cfg.readiness()
        if not ready:
            raise Stop(reason)
        return cfg, self.cfg_path.read_bytes()

    def model_path(self, name):
        if name not in ('tiny', 'base', 'small'):
            raise Stop('Only standard multilingual tiny/base/small are supported, not .en or quantized variants.')
        return self.models_dir / self.catalog['models'][name]['filename']

    def verify_model(self, name: str, path: Path | None = None):
        path = path or self.model_path(name)
        spec = self.catalog['models'][name]
        reject_symlinks(path)
        if not path.is_file() or path.stat().st_size != spec['bytes']:
            raise Stop(f'{name}: wrong/missing model size. Run prepare {name}; do not bypass verification.')
        with path.open('rb') as file:
            magic = file.read(4)
        if magic != struct.pack('<I', 0x67676d6c):
            raise Stop(f'{name}: not the expected ggml file (possibly an HTML error page).')
        digest = sha256(path)
        if digest != spec['sha256']:
            raise Stop(f'{name}: SHA256 mismatch. Existing files/config were not replaced.')
        return digest

    def prepare(self, name: str):
        self.read_config()
        target = self.model_path(name)
        reject_symlinks(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_lock(self.work / '.prepare.lock'):
            if target.exists():
                self.verify_model(name)
                print(f'MODEL READY: {target.name} (existing SHA256 verified)')
                return
            spec = self.catalog['models'][name]
            if shutil.disk_usage(target.parent).free < spec['bytes'] + 128 * 1024 * 1024:
                raise Stop('Insufficient free space for the model plus a 128 MiB margin.')
            url = ('https://huggingface.co/' + self.catalog['repository'] + '/resolve/' +
                   self.catalog['revision'] + '/' + spec['filename'])
            fd, tmp = tempfile.mkstemp(prefix='.' + target.name + '-', suffix='.part', dir=target.parent)
            os.close(fd)
            temp = Path(tmp)
            try:
                print(f'Downloading {name}: {spec["bytes"]:,} bytes. Existing models/config remain unchanged.', flush=True)
                subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                    '--proto', '=https', '--proto-redir', '=https', '--tlsv1.2',
                    '--retry', '3', '--retry-delay', '2', '--connect-timeout', '20',
                    '--max-time', '1800', '--max-filesize', str(spec['bytes']),
                    '--output', str(temp), url], check=True)
                self.verify_model(name, temp)
                if target.exists():
                    raise Stop('Model appeared during download. No existing model was replaced.')
                os.chmod(temp, 0o600)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            atomic_write(self.work / (name + '-download.json'), data_json({
                'model': name, 'sha256': spec['sha256'], 'bytes': spec['bytes'],
                'revision': self.catalog['revision'], 'verified_at': stamp()}))
            print(f'MODEL READY: {target.name} (upstream SHA256 verified). Active config unchanged.')

    def database(self):
        path = self.data / 'room-hub.sqlite3'
        # Exact app filename is checked below; never guess/open a different DB.
        return path

    @contextlib.contextmanager
    def db_read(self):
        path = self.database()
        reject_symlinks(path)
        if not path.is_file():
            raise Stop('Room Hub database not found in data/. No database was created.')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()

    def ensure_idle(self):
        with self.db_read() as db:
            n = db.execute("SELECT COUNT(*) FROM speech_jobs WHERE status IN ('queued','running')").fetchone()[0]
        if n:
            raise Stop('Queued/running transcription remains. Restart the server, finish/cancel it, then stop again.')

    @contextlib.contextmanager
    def maintenance(self):
        port_closed()
        with exclusive_lock(self.data / '.v35-server.lock'):
            with exclusive_lock(self.work / '.experiment.lock'):
                port_closed()
                self.ensure_idle()
                yield

    def candidate(self, cfg, name, timeout=None):
        self.verify_model(name)
        updates = {'model': str(self.model_path(name)), 'model_name': f'{name} · multilingual'}
        if timeout is not None:
            if not 20 <= timeout <= 600:
                raise Stop('Timeout must be 20..600 seconds; it is a limit, not an expected processing time.')
            updates['timeout_seconds'] = timeout
        return dataclasses.replace(cfg, **updates)

    async def run_audio(self, source: Path, cfg):
        start = time.monotonic()
        text, duration = await WhisperRunner().transcribe(source, cfg, asyncio.Event(), self.work / 'tmp')
        elapsed = time.monotonic() - start
        return {'text': text, 'audio_seconds': round(duration, 4),
                'elapsed_seconds': round(elapsed, 4), 'rtf': round(elapsed / duration, 4)}

    async def smoke(self, cfg):
        sample = self.root / 'runtime/stt/whisper.cpp/samples/jfk.wav'
        reject_symlinks(sample)
        if not sample.is_file():
            raise Stop('Installed upstream jfk.wav is missing. No config changed. Inspect the engine folder.')
        trial = dataclasses.replace(cfg, language='en', max_seconds=max(30, cfg.max_seconds))
        output = await self.run_audio(sample, trial)
        if not any(word in output['text'].casefold() for word in ('country', 'nation', 'fellow', 'american')):
            raise Stop('Actual engine output failed the English sample keyword sanity check; no config changed.')
        return {k: v for k, v in output.items() if k != 'text'} | {
            'scope': 'Actual installed CLI via production FFmpeg/WhisperRunner. Public English smoke only; not Korean accuracy.',
            'language': 'en', 'threads': cfg.threads, 'timeout_seconds': cfg.timeout_seconds}

    def save_config(self, cfg, old_raw: bytes, event: str, smoke: dict):
        if self.cfg_path.read_bytes() != old_raw:
            raise Stop('Configuration changed during the test. Nothing was overwritten.')
        backups = self.work / 'config-backups'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        original = backups / 'original.json'
        proof = backups / 'original.sha256'
        if not original.exists():
            if proof.exists():
                raise Stop('Incomplete original backup; inspect before proceeding.')
            atomic_write(original, old_raw)
            atomic_write(proof, (hashlib.sha256(old_raw).hexdigest() + '\n').encode())
        else:
            self.original_bytes()  # require intact baseline before adding another change
        run_id = stamp() + '-' + uuid.uuid4().hex[:8]
        atomic_write(backups / (run_id + '.json'), old_raw)
        record = {'kit_version': KIT_VERSION, 'event': event, 'model': cfg.model_name,
                  'threads': cfg.threads, 'language': cfg.language, 'smoke': smoke,
                  'previous_config_sha256': hashlib.sha256(old_raw).hexdigest(),
                  'candidate_config_sha256': hashlib.sha256(data_json(dataclasses.asdict(cfg))).hexdigest(),
                  'stage': 'validated; candidate hash can be compared with current config', 'time': stamp()}
        atomic_write(self.work / (run_id + '-switch.json'), data_json(record))
        # Last step is the atomic live-config commit. No global speech disable on failure.
        if self.cfg_path.read_bytes() != old_raw:
            raise Stop('Configuration changed just before commit; no live config was overwritten.')
        atomic_write(self.cfg_path, data_json(dataclasses.asdict(cfg)))
        print(f'MODEL ACTIVE: {cfg.model_name}; language={cfg.language}; threads={cfg.threads}; timeout={cfg.timeout_seconds}s')
        print('Actual engine smoke passed. Start Room Hub, then test Korean speech on iPad.')
        print('Config backup: data/speech-model-tests/config-backups/original.json')

    def original_bytes(self):
        folder = self.work / 'config-backups'
        original, proof = folder / 'original.json', folder / 'original.sha256'
        reject_symlinks(original); reject_symlinks(proof)
        if not original.is_file() or not proof.is_file():
            raise Stop('No complete original config backup. For this upgrade, use switch base after inspecting current status.')
        raw = original.read_bytes()
        if hashlib.sha256(raw).hexdigest() != proof.read_text().strip():
            raise Stop('Original config backup checksum mismatch; it was not used.')
        return raw

    async def switch(self, name, timeout=None):
        with self.maintenance():
            cfg, raw = self.read_config()
            # Before the first Base -> Small upgrade, preserve the exact known-good Base config.
            if name == 'small':
                if Path(cfg.model).name != 'ggml-base.bin':
                    raise Stop('First small activation expects the currently active verified multilingual base model.')
                self.verify_model('base', Path(cfg.model))
                baseline = self.work / 'small-upgrade/base-before-small.json'
                proof = self.work / 'small-upgrade/base-before-small.sha256'
                if not baseline.exists():
                    atomic_write(baseline, raw)
                    atomic_write(proof, (hashlib.sha256(raw).hexdigest() + '\n').encode())
                elif not proof.is_file() or hashlib.sha256(baseline.read_bytes()).hexdigest() != proof.read_text().strip():
                    raise Stop('Base rollback snapshot is incomplete or modified; inspect before switching.')
            candidate = self.candidate(cfg, name, timeout)
            print(f'Testing actual {name} model before touching config. This can take time on V35.', flush=True)
            smoke = await self.smoke(candidate)
            self.save_config(candidate, raw, 'switch-' + name, smoke)

    async def restore(self):
        with self.maintenance():
            reject_symlinks(self.cfg_path)
            if not self.cfg_path.is_file():
                raise Stop('Current config is absent; inspect before restoring.')
            old_raw = self.cfg_path.read_bytes()
            baseline = self.work / 'small-upgrade/base-before-small.json'
            proof = self.work / 'small-upgrade/base-before-small.sha256'
            if not baseline.is_file() or not proof.is_file():
                raise Stop('No verified Base-before-Small snapshot. Use switch base only after inspecting status.')
            raw = baseline.read_bytes()
            if hashlib.sha256(raw).hexdigest() != proof.read_text().strip():
                raise Stop('Base rollback snapshot checksum mismatch; it was not used.')
            cfg = SpeechConfig.read(baseline)
            if Path(cfg.model).name != 'ggml-base.bin' or cfg.threads != 8 or not cfg.enabled or cfg.language != 'ko':
                raise Stop('Saved rollback config is not the expected Base + 8-thread Korean configuration.')
            self.verify_model('base', Path(cfg.model))
            ready, reason = cfg.readiness()
            if not ready:
                raise Stop(reason)
            smoke = await self.smoke(cfg)
            self.save_config(cfg, old_raw, 'restore-base-before-small', smoke)

    def samples(self, limit):
        with self.db_read() as db:
            rows = db.execute("SELECT id,created_at,media_type,status FROM voice WHERE kind='audio' ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
        print('Stored input IDs and dates only; transcript content is not printed:')
        for row in rows:
            print(row['id'], row['created_at'], row['media_type'], row['status'])
        if not rows:
            print('No stored audio. Record on iPad and retain the inbox entry first.')

    def audio_source(self, file=None, latest=False, voice_id=None):
        if file:
            source = Path(file)
            if not source.is_absolute():
                source = self.root / source
            reject_symlinks(source)
            source = source.resolve()
            if not source.is_relative_to(self.root):
                raise Stop('Test input must be inside the bound project (e.g. data/model-input/test.wav).')
            label = {'kind': 'private file', 'filename': source.name}
        else:
            with self.db_read() as db:
                if latest:
                    row = db.execute("SELECT id,filename,created_at FROM voice WHERE kind='audio' ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
                else:
                    row = db.execute("SELECT id,filename,created_at FROM voice WHERE kind='audio' AND id=?", (voice_id,)).fetchone()
            if not row or not row['filename'] or Path(row['filename']).name != row['filename'] or row['filename'] in ('.','..'):
                raise Stop('Stored audio is missing/invalid; choose a retained iPad recording.')
            source = self.data / 'audio' / row['filename']
            reject_symlinks(source)
            label = {'kind': 'stored iPad/inbox audio', 'voice_id': row['id'], 'created_at': row['created_at']}
        cfg, _ = self.read_config()
        if not source.is_file() or source.stat().st_size > cfg.max_bytes:
            raise Stop('Input audio is missing or exceeds the configured upload size limit.')
        return source, label

    def load_reference(self, file):
        if file is None:
            return None
        path = Path(file)
        if not path.is_absolute():
            path = self.root / path
        reject_symlinks(path)
        if not path.resolve().is_relative_to(self.root) or not path.is_file() or path.stat().st_size > 16384:
            raise Stop('Reference must be a UTF-8 text file within the project, at most 16 KiB.')
        text = path.read_text('utf-8-sig').strip()
        char_error_rate(text, '')
        return text

    async def compare(self, args):
        with self.maintenance():
            cfg, raw = self.read_config()
            source, label = self.audio_source(args.file, args.latest, args.voice_id)
            reference = self.load_reference(args.reference_file)
            candidates = {name: self.candidate(cfg, name, args.timeout) for name in ('base', 'small')}
            result = {'kit_version': KIT_VERSION, 'created_at': stamp(), 'source': label,
                      'audio_sha256': sha256(source), 'engine_sha256': sha256(Path(cfg.binary)),
                      'model_sha256': {name: self.catalog['models'][name]['sha256'] for name in ('base','small')},
                      'architecture': platform.machine(), 'language': cfg.language, 'threads': cfg.threads,
                      'timeout_seconds': candidates['small'].timeout_seconds, 'runs_per_model': args.runs,
                      'source_reference': reference,
                      'metric_note': 'RTF=FFmpeg+model-load+inference elapsed/audio length. No upload/queue delay. '
                                     'Each run starts a new CLI, matching production. Models alternate order; no warmup. '
                                     'CER (if supplied) uses NFC/casefold; excludes punctuation/whitespace; may exceed 1. '
                                     'Base model output is NEVER treated as reference; use a supplied ground-truth file for CER.',
                      'runs': []}
            for round_idx in range(args.runs):
                order = ('base', 'small') if round_idx % 2 == 0 else ('small', 'base')
                for name in order:
                    print(f'TEST {round_idx + 1}/{args.runs}: {name} (one process only)', flush=True)
                    item = {'model': name, 'round': round_idx + 1}
                    try:
                        output = await self.run_audio(source, candidates[name])
                        item.update(output, ok=True)
                        if reference is not None:
                            item['error_metric'] = char_error_rate(reference, output['text'])
                    except (TranscriptionError, OSError, Stop) as exc:
                        item.update(ok=False, error=str(exc))
                    result['runs'].append(item)
                    print(f'{name}: ' + (f'{item["elapsed_seconds"]:.2f}s / audio {item["audio_seconds"]:.2f}s / RTF {item["rtf"]:.2f}' if item['ok'] else 'FAILED; see private report'), flush=True)
                    if args.cooldown:
                        await asyncio.sleep(args.cooldown)
            if self.cfg_path.read_bytes() != raw:
                raise Stop('Configuration changed externally during comparison. Review it before restarting.')
            result['config_unchanged'] = True
            result['ok'] = all(row['ok'] for row in result['runs'])
            result['summary'] = {}
            for name in ('base','small'):
                good = [row for row in result['runs'] if row['model'] == name and row['ok']]
                result['summary'][name] = {'successful_runs': len(good)}
                if good:
                    result['summary'][name].update(median_elapsed_seconds=statistics.median(r['elapsed_seconds'] for r in good), median_rtf=statistics.median(r['rtf'] for r in good))
            report_id = 'compare-' + stamp() + '-' + uuid.uuid4().hex[:8]
            content = render_report(result).encode('utf-8')
            atomic_write(self.work / (report_id + '.json'), data_json(result))
            atomic_write(self.work / (report_id + '.html'), content)
            atomic_write(self.work / 'latest-compare.json', data_json(result))
            atomic_write(self.work / 'latest-compare.html', content)
            print('PRIVATE REPORT: data/speech-model-tests/latest-compare.html')
            print('Config, inbox text and task database were NOT changed. Start Room Hub again.')
            return result['ok']

    def status(self, verify=False):
        cfg, _ = self.read_config()
        output = {k: getattr(cfg, k) for k in ('enabled','model_name','language','threads','timeout_seconds','max_seconds','max_pending')}
        output['model_file'] = Path(cfg.model).name
        output['room_hub_version'] = (self.root / 'VERSION').read_text('utf-8').strip()
        output['toolkit'] = KIT_VERSION
        output['installed_files'] = {}
        for name in ('base','small'):
            path = self.model_path(name)
            state = {'exists': path.is_file(), 'sha256_verified': False}
            if verify and path.is_file():
                self.verify_model(name)
                state['sha256_verified'] = True
            output['installed_files'][name] = state
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return output


def render_report(result):
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for item in result['runs']:
        metric = item.get('error_metric')
        cer = f'{metric["cer"] * 100:.1f}%' if metric else '정답 미제공'
        timing = f'{item["elapsed_seconds"]:.2f}초 · RTF {item["rtf"]:.2f}' if item['ok'] else '실패'
        body = item.get('text') if item['ok'] else item.get('error','실패')
        rows.append(f'<article><h2>{esc(item["model"])} · {item["round"]}회</h2><p>{esc(timing)} / CER {esc(cer)}</p><pre>{esc(body)}</pre></article>')
    ref = '<section><h2>사용자가 제공한 정답</h2><pre>' + esc(result['source_reference']) + '</pre></section>' if result.get('source_reference') else '<p>정답 문장이 없어 정확도 점수는 계산하지 않았습니다. 전사문을 직접 비교하세요.</p>'
    summary = ''.join(f'<tr><td>{esc(name)}</td><td>{val["successful_runs"]}</td><td>{esc(round(val["median_elapsed_seconds"],2)) if "median_elapsed_seconds" in val else "—"}</td><td>{esc(round(val["median_rtf"],2)) if "median_rtf" in val else "—"}</td></tr>' for name,val in result['summary'].items())
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Room Hub · Base / Small 비교</title>
<style>body{{font:16px/1.65 system-ui,sans-serif;max-width:1000px;margin:32px auto;padding:0 20px;background:#f5f5f2;color:#20282b}}h1{{font-size:30px}}.badge{{color:#a32424}}article,section{{background:white;border:1px solid #d9dedb;border-radius:14px;padding:18px;margin:16px 0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}}pre{{white-space:pre-wrap;word-break:break-word;font:inherit}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{text-align:left;border-bottom:1px solid #ddd;padding:10px}}small{{color:#566269}}</style>
<h1>같은 녹음, Base / Small</h1><p class="badge">개인 음성 전사문 포함 · GitHub/공개 채팅에 올리지 마세요.</p>
<p>{esc(result['created_at'])} · 한국어 · {result['threads']} threads · 모델별 {result['runs_per_model']}회</p>
<p>실행 환경은 이 보고서를 생성한 장치입니다. 기본 명령은 V35에서 실행됩니다. 결과는 한 녹음의 비교이지 일반적인 한국어 정확도 평가가 아닙니다.</p>
<table><thead><tr><th>모델</th><th>성공 횟수</th><th>중앙 처리 시간(초)</th><th>중앙 RTF</th></tr></thead><tbody>{summary}</tbody></table>
<p>RTF = 변환·모델 로딩·추론 시간 ÷ 녹음 길이. 낮을수록 빠릅니다. 업로드·서버 대기열 시간은 제외됩니다. 앱처럼 매회 새 CLI를 실행하며, 모델 순서는 번갈아 배치합니다. 발열·캐시 영향은 남습니다.</p>
{ref}<div class="grid">{''.join(rows)}</div><p>날짜·숫자·이름·할 일 내용이 의도대로 인식됐는지 확인하세요. CER는 NFC 정규화 후 공백·문장부호를 제외하고 계산하며, 낮을수록 정답에 가깝습니다. 삽입 오류가 많으면 100%를 넘을 수 있습니다.</p>
<small>설정과 수신함은 변경하지 않았습니다. 내용은 HTML escape 처리되며 외부 스크립트·폰트·추적 코드는 없습니다.</small></html>'''


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status').add_argument('--verify', action='store_true')
    sub.add_parser('prepare').add_argument('model', choices=['tiny','base','small'])
    switch = sub.add_parser('switch'); switch.add_argument('model', choices=['tiny','base','small']); switch.add_argument('--timeout', type=int)
    sub.add_parser('restore')
    sub.add_parser('samples').add_argument('--limit', type=int, default=5, choices=range(1,21))
    compare = sub.add_parser('compare')
    choice = compare.add_mutually_exclusive_group(required=True)
    choice.add_argument('--latest', action='store_true'); choice.add_argument('--voice-id'); choice.add_argument('--file')
    compare.add_argument('--reference-file'); compare.add_argument('--runs', type=int, default=1, choices=range(1,4))
    compare.add_argument('--cooldown', type=int, default=3, choices=range(0,31))
    compare.add_argument('--timeout', type=int)
    return p


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        lab = ModelLab(ROOT)
        if args.command == 'status': lab.status(args.verify)
        elif args.command == 'prepare': lab.prepare(args.model)
        elif args.command == 'switch': asyncio.run(lab.switch(args.model, args.timeout))
        elif args.command == 'restore': asyncio.run(lab.restore())
        elif args.command == 'samples': lab.samples(args.limit)
        elif args.command == 'compare': return 0 if asyncio.run(lab.compare(args)) else 1
        return 0
    except (Stop, TranscriptionError, ValueError, TypeError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print('STOP:', str(exc), file=sys.stderr)
        print('Do not reinstall/reset Room Hub. Inspect status; start the existing service after resolving the error.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInterrupted. Check active config with status; restart Room Hub when ready.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
