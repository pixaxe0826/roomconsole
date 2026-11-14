"""Bounded, single-consumer speech transcription for Room Hub.

Local/LAN is the default; a separately authorized agent may opt into cloud STT.
No command interpretation or shell interpolation. Client identity
comes from an existing server session. The optional configuration lives in data/
and is NEVER shipped with certificates, models or executable dependencies.
"""
from __future__ import annotations

import array
import asyncio
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time
import wave
from dataclasses import asdict, dataclass, fields
from typing import Awaitable, Callable

from fastapi import HTTPException
from .ai_profile import AIProfile
from .store import uid, utcnow
from .stt_accuracy import AccuracyConfig, task_hint, score_summary, diagnostic_segments, serialize_report, PATCH_ID

ACTIVE = {'queued', 'running'}
VOICE_STATUS = {'queued': 'transcription_queued', 'running': 'transcribing',
                'succeeded': 'pending_review', 'failed': 'transcription_failed',
                'cancelled': 'transcription_cancelled'}
MIMES = {'audio/mp4', 'audio/x-m4a', 'audio/webm', 'audio/ogg', 'audio/wav',
         'audio/x-wav', 'audio/wave', 'audio/mpeg', 'audio/flac', 'audio/aac'}


@dataclass(frozen=True)
class SpeechConfig:
    enabled: bool = False
    binary: str = ''
    model: str = ''
    model_name: str = 'tiny (multilingual)'
    ffmpeg: str = '/usr/bin/ffmpeg'
    language: str = 'ko'
    threads: int = 2
    max_seconds: int = 30
    timeout_seconds: int = 180
    max_pending: int = 4
    max_bytes: int = 8 * 1024 * 1024
    max_stored_bytes: int = 128 * 1024 * 1024
    backend: str = 'local_cli'
    remote_url: str = ''
    remote_model: str = ''
    remote_device: str = 'unknown'

    def validate_backend(self) -> None:
        if self.backend not in {'local_cli', 'remote_http'}:
            raise ValueError('Unknown speech backend')
        if not isinstance(self.remote_model, str) or len(self.remote_model) > 160 or any(ord(c) < 32 for c in self.remote_model):
            raise ValueError('Invalid configured remote model')
        if self.backend == 'remote_http' and (not self.remote_model.strip() or self.remote_model != self.remote_model.strip() or any(c in self.remote_model for c in '/\\')):
            raise ValueError('Supply a remote model label, not a file path')
        if self.remote_device not in {'unknown', 'cpu', 'cuda', 'vulkan', 'metal', 'openvino'}:
            raise ValueError('Invalid configured remote device')
        if not isinstance(self.remote_url, str):
            raise ValueError('Invalid remote URL')
        if self.backend == 'remote_http' or self.remote_url:
            from .remote_speech import validate_endpoint
            validate_endpoint(self.remote_url)

    @classmethod
    def read(cls, path: Path, *, codec_only: bool = False,
             ffmpeg_override: str | None = None) -> 'SpeechConfig':
        if not path.exists():
            return cls._from_mapping({}, codec_only=codec_only, ffmpeg_override=ffmpeg_override)
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError('Invalid speech configuration file')
        raw = json.loads(path.read_text('utf-8'))
        return cls._from_mapping(raw, codec_only=codec_only, ffmpeg_override=ffmpeg_override)

    @classmethod
    def _from_mapping(cls, raw, *, codec_only: bool = False,
                      ffmpeg_override: str | None = None) -> 'SpeechConfig':
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError('Invalid speech configuration fields')
        if codec_only:
            # A retired PC's model/endpoint/executable is not a prerequisite for
            # OpenAI. Keep the file unchanged, using only local codec and quota
            # fields. Invalid codec limits still fail closed.
            codec_fields = {'ffmpeg', 'language', 'max_seconds', 'timeout_seconds',
                            'max_pending', 'max_bytes', 'max_stored_bytes'}
            raw = {key: value for key, value in raw.items() if key in codec_fields}
        if ffmpeg_override is not None:
            raw = dict(raw, ffmpeg=ffmpeg_override)
        cfg = cls(**raw)
        if type(cfg.enabled) is not bool:
            raise ValueError('enabled must be boolean')
        for key, lo, hi in [('threads', 1, 8), ('max_seconds', 3, 60),
                            ('timeout_seconds', 20, 600), ('max_pending', 1, 10),
                            ('max_bytes', 1024, 10 * 1024 * 1024),
                            ('max_stored_bytes', 1024, 1024 * 1024 * 1024)]:
            value = getattr(cfg, key)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError('Invalid setting: ' + key)
        if cfg.language not in {'ko', 'en', 'ja', 'auto'}:
            raise ValueError('Unsupported language')
        if not isinstance(cfg.model_name, str) or len(cfg.model_name) > 100:
            raise ValueError('Invalid model name')
        for value in (cfg.binary, cfg.model, cfg.ffmpeg):
            if not isinstance(value, str) or ('\x00' in value) or (value and not Path(value).is_absolute()):
                raise ValueError('Runtime paths must be absolute')
        cfg.validate_backend()
        return cfg

    def readiness(self) -> tuple[bool, str]:
        try:
            self.validate_backend()
        except (ValueError, TypeError):
            return False, '전사 backend 또는 원격 연결 설정을 확인하세요.'
        if not self.enabled:
            if self.backend == 'remote_http':
                return False, '원격 전사 기능이 꺼져 있습니다. 서버 설정을 확인하세요.'
            return False, '로컬 전사 엔진을 먼저 설치하세요. 서버의 install-speech.sh를 실행합니다.'
        if self.backend == 'local_cli' and (not self.binary or not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK)):
            return False, 'whisper-cli 실행 파일이 준비되지 않았습니다.'
        if self.backend == 'local_cli' and (not self.model or not Path(self.model).is_file() or Path(self.model).stat().st_size < 1024):
            return False, '다국어 Whisper 모델 파일을 확인하세요.'
        if not Path(self.ffmpeg).is_file() or not os.access(self.ffmpeg, os.X_OK):
            return False, 'FFmpeg를 먼저 설치하세요.'
        return True, ''


class TranscriptionError(Exception):
    """Only safe, user-facing explanations; never include child stdout/stderr."""


async def _run_process(args: list[str], cancel: asyncio.Event, timeout: float) -> None:
    """A private process group, bounded time, no shell; tear down on cancellation."""
    if cancel.is_set():
        raise asyncio.CancelledError
    command = ([shutil.which('nice'), '-n', '10'] if os.name == 'posix' and shutil.which('nice') else []) + args
    proc = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=(os.name == 'posix'))
    waiter = asyncio.create_task(proc.wait())
    deadline = time.monotonic() + timeout
    try:
        while proc.returncode is None:
            if cancel.is_set():
                raise asyncio.CancelledError
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TranscriptionError('처리 제한 시간을 넘었습니다. 더 짧게 녹음하거나 tiny 모델을 사용하세요.')
            try:
                await asyncio.wait_for(asyncio.shield(waiter), min(.2, remaining))
            except asyncio.TimeoutError:
                pass
        if proc.returncode != 0:
            raise TranscriptionError('음성 변환 또는 전사에 실패했습니다. 파일·모델·실행 환경을 확인하세요.')
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == 'posix':
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    if os.name == 'posix':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                await proc.wait()


def inspect_wav(path: Path, max_seconds: int) -> float:
    try:
        with wave.open(str(path), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
                raise TranscriptionError('PCM 변환 결과를 확인할 수 없습니다.')
            duration = wav.getnframes() / 16000
            if duration < .25:
                raise TranscriptionError('녹음이 너무 짧습니다. 1초 이상 말한 뒤 다시 보내세요.')
            if duration > max_seconds + .15:
                raise TranscriptionError(f'녹음은 최대 {max_seconds}초입니다. 파일이 너무 깁니다.')
            samples = array.array('h', wav.readframes(wav.getnframes()))
            if sys.byteorder != 'little':
                samples.byteswap()
            rms = math.sqrt(sum(x*x for x in samples) / max(1, len(samples)))
            if rms < 20:
                raise TranscriptionError('녹음에서 소리를 확인하지 못했습니다. 마이크 권한과 거리를 확인하세요.')
            return duration
    except (wave.Error, EOFError, OSError) as exc:
        raise TranscriptionError('유효한 음성 파일로 변환할 수 없습니다.') from exc


async def prepare_wav(source: Path, cfg: SpeechConfig, cancel: asyncio.Event, wav: Path) -> float:
    """Unchanged local codec/rate/duration/silence checks, shared by both backends."""
    await _run_process([cfg.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-protocol_whitelist', 'file,pipe', '-format_whitelist',
        'mov,matroska,webm,wav,mp3,ogg,flac,aac', '-threads', '1', '-i', str(source),
        '-map', '0:a:0', '-vn', '-sn', '-dn', '-t', str(cfg.max_seconds + 1),
        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-threads', '1',
        '-y', str(wav)], cancel, 20)
    return await asyncio.to_thread(inspect_wav, wav, cfg.max_seconds)


class WhisperRunner:
    async def transcribe(self, source: Path, cfg: SpeechConfig, cancel: asyncio.Event,
                         temp_root: Path) -> tuple[str, float]:
        # Stable legacy interface for model smoke/comparison tools. Production
        # opts in explicitly through SpeechHub; do not alter those tools silently.
        text, duration, _ = await self.transcribe_detailed(
            source, cfg, cancel, temp_root, AccuracyConfig(), {'text': '', 'titles': []})
        return text, duration

    async def transcribe_detailed(self, source: Path, cfg: SpeechConfig, cancel: asyncio.Event,
                                 temp_root: Path, policy: AccuracyConfig, hint: dict) -> tuple[str, float, dict]:
        temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        total_start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='job-', dir=temp_root) as folder:
            wav = Path(folder) / 'input.wav'
            # Capture, codec handling, rate, gain, max duration and silence check
            # are identical to 0.1.5. No VAD/filter/AGC/microphone changes.
            conversion_start = time.monotonic()
            duration = await prepare_wav(source, cfg, cancel, wav)
            conversion_seconds = time.monotonic() - conversion_start
            out = Path(folder) / 'transcript'
            args = [cfg.binary, '-m', cfg.model, '-f', str(wav), '-l', cfg.language,
                '-t', str(cfg.threads), '-p', '1', '-ng', '-nt', '-np', '-otxt', '-of', str(out),
                '-bo', '1', '-bs', str(policy.beam)]
            if policy.profile != 'legacy':
                # Plain JSON + scores avoids -ojf's extra token-timestamp work.
                args += ['-oj', '-ls']
                if hint.get('text'):
                    args += ['--prompt', hint['text']]
            inference_start = time.monotonic()
            await _run_process(args, cancel, cfg.timeout_seconds)
            inference_seconds = time.monotonic() - inference_start
            result = out.with_suffix('.txt')
            if not result.is_file() or result.stat().st_size > 128 * 1024:
                raise TranscriptionError('전사 결과 파일이 없거나 너무 큽니다.')
            text = result.read_text('utf-8').strip()
            if not text:
                raise TranscriptionError('인식된 말이 없습니다. 더 가까이에서 또렷하게 다시 녹음하세요.')
            text = text[:16000]
            report = {
                'schema': 1, 'patch_id': PATCH_ID, 'profile': policy.profile,
                'runtime_patch_id': 'room-hub-m3.4-0', 'backend': 'local_cli',
                'model_identity_source': 'local_cli_argument', 'device': 'cpu',
                'beam_size': policy.beam, 'best_of': 1, 'temperature_fallback': 'engine default (unchanged)',
                'model_file': Path(cfg.model).name, 'model_name': cfg.model_name,
                'threads': cfg.threads, 'language': cfg.language, 'cpu_only': True,
                'hint': hint, 'raw_transcript': text, 'selected_transcript': text,
                'postprocessing': 'none; CLI .txt is canonical', 'passes': 1,
                'automatic_second_pass': False, 'audio_seconds': duration,
                'conversion_seconds': conversion_seconds,
                'whisper_seconds_including_load': inference_seconds,
                'runner_total_seconds': time.monotonic() - total_start,
                'scores': score_summary(out.with_suffix('.score.txt')),
                'segments': diagnostic_segments(out.with_suffix('.json')),
                'notes': ['Token scores are not calibrated accuracy.',
                          'A prompt can bias output; audio evidence must be reviewed.',
                          'No LLM call, action execution or raw-audio edits in STT.'],
            }
            return text, duration, report


def validate_audio(blob: bytes, mime: str) -> None:
    if mime not in MIMES:
        raise HTTPException(415, 'MP4/M4A, WebM, WAV, MP3, OGG, FLAC, AAC 음성만 받습니다.')
    magic = (blob[:4] in {b'RIFF', b'OggS', b'fLaC', b'\x1aE\xdf\xa3'} or
             blob[4:8] == b'ftyp' or blob[:3] == b'ID3' or
             len(blob) > 2 and blob[0] == 0xff and (blob[1] & 0xe0) == 0xe0)
    if len(blob) < 12 or not magic:
        raise HTTPException(415, '녹음 파일 형식을 확인할 수 없습니다. 다시 녹음하세요.')


class SpeechHub:
    def __init__(self, store, data: Path, changed: Callable[..., Awaitable[None]],
                 config: SpeechConfig | None = None, runner=None, profile=None):
        self.store, self.data, self.changed = store, data, changed
        self.profile = profile or AIProfile.from_env()
        self.ffmpeg_override = (os.environ.get('HUB_FFMPEG') or None) if self.profile.pc_free else None
        self.override = config
        self.runner = runner or WhisperRunner()
        self.runner_override = runner is not None
        from .remote_speech import RemoteWhisperRunner
        self.remote_runner = RemoteWhisperRunner()
        self.probe_lock = asyncio.Lock()
        self.last_probe = None
        self._probe_config = None
        self._probe_time = None
        self.wake = asyncio.Event()
        self.task = None
        self.active_id = None
        self.active_cancel = None
        self.last_error = ''
        self.on_transcribed = None
        self.agent_options = None
        self.cloud_transcriber = None
        self.agent_viewer = None
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS speech_jobs (
                id TEXT PRIMARY KEY, voice_id TEXT NOT NULL UNIQUE REFERENCES voice(id) ON DELETE CASCADE,
                owner TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL,
                digest TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1,
                duration REAL, elapsed REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(owner,request_id))''')
            db.execute('CREATE INDEX IF NOT EXISTS speech_jobs_queue ON speech_jobs(status,created_at)')
            if 'stt_backend' not in {r['name'] for r in db.execute('PRAGMA table_info(speech_jobs)')}:
                db.execute("ALTER TABLE speech_jobs ADD COLUMN stt_backend TEXT NOT NULL DEFAULT 'configured'")
            db.execute('''CREATE TABLE IF NOT EXISTS speech_diagnostics (
                job_id TEXT NOT NULL REFERENCES speech_jobs(id) ON DELETE CASCADE,
                attempt INTEGER NOT NULL, report TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(job_id,attempt))''')

    def set_transcript_sink(self, callback: Callable[[str, str, str], Awaitable[object]] | None) -> None:
        """Connect successful STT to the existing assistant request pipeline."""
        self.on_transcribed = callback

    def set_agent_hooks(self, *, options, transcribe, viewer):
        self.agent_options, self.cloud_transcriber, self.agent_viewer = options, transcribe, viewer

    def _readiness(self, cfg):
        options = self.agent_options() if self.agent_options is not None else {}
        if options.get('cloud_stt_available') and self.cloud_transcriber is not None:
            ready = bool(Path(cfg.ffmpeg).is_file() and os.access(cfg.ffmpeg, os.X_OK))
            return ready, '' if ready else '외부 음성 전송 전 검증에 필요한 FFmpeg를 확인하세요.', options
        if self.profile.pc_free:
            reasons = {
                'agent_disabled': 'Personal Agent를 활성화하세요.',
                'cloud_disabled': 'OpenAI 클라우드 플래너 허용 정책을 확인하세요.',
                'audio_cloud_disabled': '음성 외부 OpenAI STT 전송 허용 정책을 확인하세요.',
                'missing_api_key': 'Odyssey 서비스의 OpenAI API 키를 설정하세요.',
                'api_key_missing': 'Odyssey 서비스의 OpenAI API 키를 설정하세요.',
                'budget_hard_cap_reached': 'OpenAI 월 예산 한도에 도달했습니다.',
                'budget_exhausted': 'OpenAI 월 예산 한도에 도달했습니다.',
            }
            reason = reasons.get(options.get('reason'), 'OpenAI 전사 설정과 준비 상태를 확인하세요.')
            return False, reason + ' PC/로컬 Whisper로 대체하지 않습니다.', options
        ready, reason = cfg.readiness()
        return ready, reason, options

    def config(self) -> SpeechConfig:
        if self.override is not None:
            if self.profile.pc_free:
                return SpeechConfig._from_mapping(asdict(self.override), codec_only=True,
                                                   ffmpeg_override=self.ffmpeg_override)
            return self.override
        return SpeechConfig.read(self.data / 'speech-config.json', codec_only=self.profile.pc_free,
                                 ffmpeg_override=self.ffmpeg_override)

    def status(self) -> dict:
        try:
            cfg = self.config()
            accuracy = AccuracyConfig() if self.profile.pc_free else AccuracyConfig.read(self.data / 'stt-accuracy.json')
            ready, reason, agent = self._readiness(cfg)
        except (ValueError, TypeError, OSError, json.JSONDecodeError):
            cfg = SpeechConfig()
            accuracy = AccuracyConfig()
            ready, reason = False, ('음성 코덱 설정 또는 HUB_FFMPEG를 확인하세요.' if self.profile.pc_free
                                    else 'speech-config.json 또는 stt-accuracy.json 설정을 확인하세요.')
            agent = {}
        if ready and (self.task is None or self.task.done()):
            ready, reason = False, ('정책 적용 또는 Odyssey의 Room Hub 서비스 재시작으로 음성 작업자를 시작하세요.'
                                    if self.profile.pc_free else '엔진 설치 후 Room Hub 서비스를 다시 시작하세요.')
        with self.store.connect() as db:
            counts = dict(db.execute('SELECT status,COUNT(*) FROM speech_jobs GROUP BY status').fetchall())
        cloud = self.profile.pc_free or bool(agent.get('cloud_stt_available'))
        return {'ready': ready, 'enabled': bool(agent.get('cloud_stt_available')) if self.profile.pc_free else cfg.enabled or cloud,
                'engine': 'openai' if cloud else 'whisper.cpp',
                **self.profile.public(),
                'legacy_backend_allowed': not self.profile.pc_free,
                'agent_enabled': bool(agent.get('agent_enabled')),
                'model': agent.get('stt_model') if cloud else (cfg.remote_model or cfg.model_name) if cfg.backend == 'remote_http' else cfg.model_name,
                'backend': 'openai' if cloud else cfg.backend, 'model_identity_source': 'operator_config_not_verified' if cloud or cfg.backend == 'remote_http' else 'local_cli_argument',
                'language': cfg.language, 'threads': None if cloud or cfg.backend == 'remote_http' else cfg.threads,
                'max_seconds': cfg.max_seconds, 'max_bytes': cfg.max_bytes,
                'max_pending': cfg.max_pending, 'queued': counts.get('queued', 0),
                'running': counts.get('running', 0), 'reason': reason,
                'auto_execute': False, 'auto_submit_llm': self.on_transcribed is not None,
                'local_only': not cloud and cfg.backend == 'local_cli', 'loopback_transport_only': not cloud,
                'readiness_scope': 'local_prerequisites_and_worker; not remote availability',
                'accuracy': accuracy.public()}

    async def probe(self):
        """Manager-only caller. No audio sent, no model load/restart or retry."""
        if self.profile.pc_free:
            state = self.status()
            return {'ok': state['ready'], 'reachable': None, 'checked_at': utcnow(),
                    'backend': 'openai', 'state': 'configured' if state['ready'] else 'unavailable',
                    'network_checked': False, 'model_verified': False,
                    'message': state['reason'] or 'Odyssey의 OpenAI 전사 준비 조건만 확인했습니다. 실제 API 호출은 하지 않았습니다.'}
        if self.probe_lock.locked():
            raise HTTPException(409, '전사 연결 확인이 진행 중입니다.')
        async with self.probe_lock:
            cfg = self.config()
            ready, reason = cfg.readiness()
            if not ready or cfg.backend == 'local_cli':
                result = {'ok': ready, 'reachable': None, 'checked_at': utcnow(),
                          'state': 'configured' if ready else 'unavailable',
                          'message': reason or '로컬 실행 파일/모델 경로 확인. 실제 전사는 별도 검사입니다.'}
            else:
                result = await self.remote_runner.probe(cfg)
            # A settings-file edit during the request must not verify a new endpoint.
            if cfg != self.config():
                raise HTTPException(409, '확인 중 전사 설정이 바뀌었습니다. 다시 확인하세요.')
            self.last_probe, self._probe_config = result, cfg
            self._probe_time = time.monotonic()
            return result

    def connection_status(self):
        if self.profile.pc_free:
            state = self.status()
            return {'backend': 'openai', 'enabled': state['enabled'], 'ready': state['ready'],
                    'runtime_profile': self.profile.name, 'pc_required': False,
                    'configured_model': state['model'], 'reported_model': None, 'model_verified': False,
                    'configured_device': 'cloud', 'reported_device': None,
                    'identity_note': '로컬 설정/예산 확인이며 OpenAI 가용성·실제 전사 성공의 증거가 아닙니다.',
                    'state': 'configured' if state['ready'] else 'unavailable',
                    'message': state['reason'], 'last_probe': None, 'stale': False,
                    'network_checked': False, 'stale_after_seconds': None}
        cfg = self.config()
        fresh = (cfg == self._probe_config and self._probe_time is not None
                 and time.monotonic() - self._probe_time <= 30)
        return {'backend': cfg.backend,
                'configured_model': (cfg.remote_model or cfg.model_name) if cfg.backend == 'remote_http' else cfg.model_name,
                'reported_model': None, 'model_verified': False,
                'configured_device': cfg.remote_device if cfg.backend == 'remote_http' else 'cpu',
                'reported_device': None,
                'identity_note': '원격 API는 모델/장치 ID를 제공하지 않습니다.' if cfg.backend == 'remote_http' else '설치 경로 확인은 실제 전사 검사가 아닙니다.',
                'state': self.last_probe['state'] if fresh else 'unknown',
                'last_probe': self.last_probe if cfg == self._probe_config else None,
                'stale': not fresh, 'stale_after_seconds': 30}

    @staticmethod
    def owner(session: dict) -> str:
        return 'admin' if session['role'] == 'admin' else 'device:' + session['device_id']

    def get(self, job_id: str, session: dict) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT j.*,v.text,v.metadata,v.status AS review_status FROM speech_jobs j JOIN voice v ON v.id=j.voice_id WHERE j.id=?', (job_id,)).fetchone()
        if not row or session['role'] != 'admin' and row['owner'] != self.owner(session):
            raise HTTPException(404, '이 기기에서 확인할 수 있는 전사 작업이 없습니다.')
        # Do not return filesystem paths, hashes, raw audio URLs or owner identifiers.
        result = {k: row[k] for k in ['id','voice_id','request_id','status','error','attempts',
                                    'duration','elapsed','created_at','updated_at','text','review_status']}
        result['stt_backend'] = row['stt_backend']
        result['agent_run'] = None
        metadata = json.loads(row['metadata'] or '{}')
        linked = metadata.get('agent_run')
        if isinstance(linked, dict) and linked.get('id') and self.agent_viewer is not None:
            result['agent_run'] = self.agent_viewer(linked['id'], session)
        elif metadata.get('agent_delivery') in {'pending', 'failed', 'interrupted'}:
            result['agent_run'] = {'status': metadata['agent_delivery'], 'summary_text':
                '개인 비서 처리 중입니다.' if metadata['agent_delivery'] == 'pending'
                else '개인 비서 전달이 중단되었습니다. 관리자에서 기록을 확인하세요. 자동 재실행하지 않습니다.'}
        # Prompt contains private task vocabulary: never expose it to displays.
        if session['role'] == 'admin':
            with self.store.connect() as db:
                diag = db.execute('SELECT report FROM speech_diagnostics WHERE job_id=? ORDER BY attempt DESC LIMIT 1', (job_id,)).fetchone()
            result['stt_diagnostics'] = json.loads(diag['report']) if diag else None
        return result

    def recent(self, session: dict) -> list:
        with self.store.connect() as db:
            if session['role'] == 'admin':
                rows = db.execute('SELECT id FROM speech_jobs ORDER BY created_at DESC LIMIT 20').fetchall()
            else:
                rows = db.execute('SELECT id FROM speech_jobs WHERE owner=? ORDER BY created_at DESC LIMIT 10', (self.owner(session),)).fetchall()
        return [self.get(r['id'], session) for r in rows]

    def require_ready(self) -> SpeechConfig:
        state = self.status()
        if not state['ready']:
            raise HTTPException(503, state['reason'])
        return self.config()

    def _capacity(self, db, cfg: SpeechConfig) -> None:
        if db.execute("SELECT COUNT(*) FROM speech_jobs WHERE status IN ('queued','running')").fetchone()[0] >= cfg.max_pending:
            raise HTTPException(429, '전사 대기열이 가득 찼습니다. 처리 후 다시 보내세요.')

    async def submit(self, blob: bytes, mime: str, request_id: str, session: dict) -> dict:
        if not re.fullmatch(r'[a-zA-Z0-9._:-]{1,128}', request_id):
            raise HTTPException(422, '올바른 요청 ID가 필요합니다.')
        mime = mime.split(';')[0].lower()
        validate_audio(blob, mime)
        owner = self.owner(session)
        digest = hashlib.sha256(blob + b'\0' + mime.encode()).hexdigest()
        # Find duplicate before checking availability/capacity: lost responses can be recovered.
        with self.store.connect() as db:
            existing = db.execute('SELECT id,digest FROM speech_jobs WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
        if existing:
            if existing['digest'] != digest:
                raise HTTPException(409, '같은 요청 ID에 다른 녹음이 들어왔습니다.')
            return {**self.get(existing['id'], session), 'duplicate': True}
        cfg = self.require_ready()
        backend = 'openai' if self.status()['backend'] == 'openai' else 'configured'
        if not 1 <= len(blob) <= cfg.max_bytes:
            raise HTTPException(413, f'녹음 파일은 최대 {cfg.max_bytes//(1024*1024)}MB입니다.')
        audio = self.data / 'audio'
        audio.mkdir(exist_ok=True, mode=0o700)
        used = sum(f.stat().st_size for f in audio.iterdir() if f.is_file())
        if used + len(blob) > cfg.max_stored_bytes or shutil.disk_usage(self.data).free < len(blob) + 64*1024*1024:
            raise HTTPException(507, '음성 저장 공간이 부족합니다. 관리자 수신함의 오래된 녹음을 삭제하세요.')
        jid, vid, now = uid(), uid(), utcnow()
        path = audio / (vid + '.bin')
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._capacity(db, cfg)
                # No await in this transaction; only one application worker is supported.
                path.write_bytes(blob)
                path.chmod(0o600)
                db.execute('''INSERT INTO voice(id,request_id,source,kind,text,locale,status,metadata,filename,media_type,digest,created_at)
                    VALUES(?,?,?,'audio','','ko-KR','transcription_queued','{}',?,?,?,?)''',
                    (vid, request_id, 'ipad.' + owner.replace(':','.'), path.name, mime, digest, now))
                # Pin explicit cloud consent at upload. Old queued local jobs do
                # not become cloud uploads just because a policy later changes.
                db.execute('''INSERT INTO speech_jobs(id,voice_id,owner,request_id,status,digest,created_at,updated_at,stt_backend)
                    VALUES(?,?,?,?,'queued',?,?,?,?)''', (jid,vid,owner,request_id,digest,now,now,backend))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.wake.set()
        await self.changed('speech.queued', jid)
        return {**self.get(jid, session), 'duplicate': False}

    async def enqueue_existing(self, voice_id: str, session: dict) -> dict:
        if session.get('role') != 'admin':
            raise HTTPException(403, '기존 음성 파일의 전사 시작은 관리자만 할 수 있습니다.')
        with self.store.connect() as db:
            prior = db.execute('SELECT id FROM speech_jobs WHERE voice_id=?', (voice_id,)).fetchone()
            row = db.execute('SELECT * FROM voice WHERE id=?', (voice_id,)).fetchone()
        if prior:
            return await self.retry(prior['id'], session)
        cfg = self.require_ready()
        if not row or row['kind'] != 'audio' or not row['filename']:
            raise HTTPException(404, '전사할 음성 파일이 없습니다.')
        path = self.data / 'audio' / Path(row['filename']).name
        if not path.is_file() or path.stat().st_size > cfg.max_bytes:
            raise HTTPException(413, '음성 파일이 없거나 전사 크기 제한을 초과합니다.')
        validate_audio(path.read_bytes(), row['media_type'])
        jid, now = uid(), utcnow()
        backend = 'openai' if self.status()['backend'] == 'openai' else 'configured'
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._capacity(db, cfg)
            db.execute('''INSERT INTO speech_jobs(id,voice_id,owner,request_id,status,digest,created_at,updated_at,stt_backend)
                VALUES(?,?,'admin',?,'queued',?,?,?,?)''', (jid,voice_id,'import.'+voice_id,row['digest'],now,now,backend))
            db.execute("UPDATE voice SET status='transcription_queued' WHERE id=?", (voice_id,))
        self.wake.set()
        await self.changed('speech.queued', jid)
        return self.get(jid, session)

    async def cancel(self, job_id: str, session: dict) -> dict:
        job = self.get(job_id, session)
        if job['status'] in ACTIVE:
            with self.store.connect() as db:
                db.execute("UPDATE speech_jobs SET status='cancelled',error='',updated_at=? WHERE id=? AND status IN ('queued','running')", (utcnow(),job_id))
                db.execute("UPDATE voice SET status='transcription_cancelled' WHERE id=?", (job['voice_id'],))
            if self.active_id == job_id and self.active_cancel:
                self.active_cancel.set()
            await self.changed('speech.cancelled', job_id)
        return self.get(job_id, session)

    async def retry(self, job_id: str, session: dict) -> dict:
        job = self.get(job_id, session)
        if self.profile.pc_free and job['stt_backend'] != 'openai':
            raise HTTPException(409, '이 녹음은 기존 PC/로컬 전사 요청입니다. 자동으로 OpenAI에 전송하지 않습니다. 새 음성 요청을 제출하세요.')
        if job['status'] in ACTIVE:
            return job  # idempotent tap, not a second job
        if job['status'] == 'succeeded':
            raise HTTPException(409, '완료된 전사는 자동으로 덮어쓰지 않습니다. 관리자가 내용을 검토하세요.')
        if self.active_id == job_id:
            raise HTTPException(409, '기존 전사 프로세스를 종료하고 있습니다. 잠시 후 재시도하세요.')
        cfg = self.require_ready()
        if job['attempts'] >= 3:
            raise HTTPException(409, '이 녹음은 3회 시도했습니다. 오류를 확인하거나 새로 녹음하세요.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._capacity(db, cfg)
            db.execute("UPDATE speech_jobs SET status='queued',error='',attempts=attempts+1,duration=NULL,elapsed=NULL,updated_at=? WHERE id=?", (utcnow(),job_id))
            db.execute("UPDATE voice SET status='transcription_queued' WHERE id=?", (job['voice_id'],))
        self.wake.set()
        await self.changed('speech.queued', job_id)
        return self.get(job_id, session)

    def ensure_not_running(self, voice_id: str):
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM speech_jobs WHERE voice_id=? AND status IN ('queued','running')", (voice_id,)).fetchone()
        if row:
            raise HTTPException(409, '전사 중입니다. 먼저 전사 취소 후 수정하거나 삭제하세요.')
        if self.active_id:
            with self.store.connect() as db:
                row = db.execute('SELECT voice_id FROM speech_jobs WHERE id=?', (self.active_id,)).fetchone()
            if row and row['voice_id'] == voice_id:
                raise HTTPException(409, '전사 프로세스가 종료 중입니다. 잠시 기다려 주세요.')

    async def start(self):
        if self.task is not None and not self.task.done():
            return
        if self.profile.pc_free:
            # Retire only in-flight legacy work, preserving source audio, exact
            # backend choice and history. Never reinterpret it as cloud consent.
            with self.store.connect() as db:
                db.execute("UPDATE voice SET status='transcription_failed' WHERE id IN (SELECT voice_id FROM speech_jobs WHERE stt_backend!='openai' AND status IN ('queued','running'))")
                db.execute("UPDATE speech_jobs SET status='failed',error=?,updated_at=? WHERE stt_backend!='openai' AND status IN ('queued','running')",
                           ('Odyssey OpenAI 모드에서 기존 PC/로컬 전사를 중단했습니다. 새 음성 요청을 제출하세요.', utcnow()))
                # Recovery must happen even when a key/policy/budget is missing.
                db.execute("UPDATE voice SET status='transcription_failed' WHERE id IN (SELECT voice_id FROM speech_jobs WHERE status='running')")
                db.execute("UPDATE speech_jobs SET status='failed',error='서버가 재시작되어 전사가 중단되었습니다. 수동으로 재시도하세요.',updated_at=? WHERE status='running'", (utcnow(),))
                self._interrupt_agent_deliveries(db)
        # A disabled/missing engine creates no worker; restart after engine installation.
        try:
            ready, _, _ = self._readiness(self.config())
        except (ValueError, TypeError, OSError):
            ready = False
        if not ready:
            return
        self.wake = asyncio.Event()
        # Do not silently re-execute audio that was interrupted during shutdown/crash.
        with self.store.connect() as db:
            db.execute("UPDATE voice SET status='transcription_failed' WHERE id IN (SELECT voice_id FROM speech_jobs WHERE status='running')")
            db.execute("UPDATE speech_jobs SET status='failed',error='서버가 재시작되어 전사가 중단되었습니다. 수동으로 재시도하세요.',updated_at=? WHERE status='running'", (utcnow(),))
            self._interrupt_agent_deliveries(db)
        self.task = asyncio.create_task(self._loop())

    @staticmethod
    def _interrupt_agent_deliveries(db):
        for voice in db.execute("SELECT id,metadata FROM voice WHERE kind='audio'").fetchall():
            metadata = json.loads(voice['metadata'] or '{}')
            if metadata.get('agent_delivery') == 'pending':
                metadata['agent_delivery'] = 'interrupted'
                db.execute('UPDATE voice SET metadata=? WHERE id=?', (json.dumps(metadata), voice['id']))

    async def close(self):
        if self.active_cancel:
            self.active_cancel.set()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _loop(self):
        while True:
            self.wake.clear()
            try:
                if self.status()['ready']:
                    with self.store.connect() as db:
                        row = db.execute("SELECT j.id,j.voice_id,j.stt_backend,v.filename FROM speech_jobs j JOIN voice v ON v.id=j.voice_id WHERE j.status='queued' ORDER BY j.created_at,j.id LIMIT 1").fetchone()
                    if row:
                        await self._process(dict(row))
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Avoid a tight crash loop and do not print untrusted transcription text.
                self.last_error = '전사 대기열 내부 오류. 관리자에서 기록을 확인하세요.'
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 2)

    async def _process(self, row: dict):
        jid, vid = row['id'], row['voice_id']
        with self.store.connect() as db:
            if not db.execute("UPDATE speech_jobs SET status='running',updated_at=? WHERE id=? AND status='queued'", (utcnow(),jid)).rowcount:
                return
            db.execute("UPDATE voice SET status='transcribing' WHERE id=?", (vid,))
        self.active_id, self.active_cancel = jid, asyncio.Event()
        start = time.monotonic()
        try:
            await self.changed('speech.running', jid)
            source = self.data / 'audio' / Path(row['filename']).name
            report = None
            cfg = self.config()
            if self.profile.pc_free:
                if row.get('stt_backend') != 'openai':
                    raise TranscriptionError('기존 PC/로컬 전사 요청은 Odyssey OpenAI 모드에서 실행하지 않습니다. 새 음성 요청을 제출하세요.')
                ready, reason, _ = self._readiness(cfg)
                if not ready:
                    raise TranscriptionError(reason)
            runner = self.remote_runner if cfg.backend == 'remote_http' and not self.runner_override else self.runner
            if row.get('stt_backend') == 'openai' and self.cloud_transcriber is not None:
                text, duration = await self.cloud_transcriber(jid, vid, source, cfg, self.active_cancel)
            elif isinstance(runner, WhisperRunner):
                policy = AccuracyConfig.read(self.data / 'stt-accuracy.json')
                hint = task_hint(self.store, policy)
                text, duration, report = await runner.transcribe_detailed(
                    source, cfg, self.active_cancel, self.data/'speech-tmp', policy, hint)
            else:
                text, duration = await runner.transcribe(source, cfg, self.active_cancel, self.data/'speech-tmp')
            if self.active_cancel.is_set():
                raise asyncio.CancelledError
            succeeded = False
            with self.store.connect() as db:
                if db.execute("UPDATE speech_jobs SET status='succeeded',duration=?,elapsed=?,updated_at=? WHERE id=? AND status='running'", (duration,time.monotonic()-start,utcnow(),jid)).rowcount:
                    db.execute("UPDATE voice SET text=?,status='pending_review' WHERE id=?", (text,vid))
                    if self.profile.pc_free or (self.agent_options is not None and self.agent_options().get('agent_enabled')):
                        voice = db.execute('SELECT metadata FROM voice WHERE id=?', (vid,)).fetchone()
                        metadata = json.loads(voice['metadata'] or '{}')
                        metadata['agent_delivery'] = 'pending'
                        db.execute('UPDATE voice SET metadata=? WHERE id=?', (json.dumps(metadata), vid))
                    if report is not None:
                        attempt = db.execute('SELECT attempts FROM speech_jobs WHERE id=?', (jid,)).fetchone()[0]
                        db.execute('INSERT INTO speech_diagnostics(job_id,attempt,report,created_at) VALUES(?,?,?,?)',
                                   (jid,attempt,serialize_report(report),utcnow()))
                    succeeded = True
            if succeeded:
                await self.changed('speech.succeeded', jid)
                if self.on_transcribed:
                    try:
                        # The sink reuses LLMHub.submit() with a deterministic key.
                        # A sink failure must never rewrite successful STT as failed.
                        result = await self.on_transcribed(jid, vid, text)
                        if isinstance(result, dict) and result.get('run_id'):
                            with self.store.connect() as db:
                                voice = db.execute('SELECT metadata FROM voice WHERE id=?', (vid,)).fetchone()
                                metadata = json.loads(voice['metadata'] or '{}')
                                metadata.update(agent_run={'id': result['run_id']}, agent_delivery='completed')
                                db.execute('UPDATE voice SET metadata=? WHERE id=?', (json.dumps(metadata), vid))
                        else:
                            with self.store.connect() as db:
                                voice = db.execute('SELECT metadata FROM voice WHERE id=?', (vid,)).fetchone()
                                metadata = json.loads(voice['metadata'] or '{}')
                                if metadata.get('agent_delivery') == 'pending':
                                    metadata['agent_delivery'] = 'completed'
                                    db.execute('UPDATE voice SET metadata=? WHERE id=?', (json.dumps(metadata), vid))
                    except Exception:
                        self.last_error = '전사는 완료됐지만 Assistant 자동 전달에 실패했습니다. 관리자 기록에서 수동 재전송하세요.'
                        with self.store.connect() as db:
                            voice = db.execute('SELECT metadata FROM voice WHERE id=?', (vid,)).fetchone()
                            metadata = json.loads(voice['metadata'] or '{}')
                            if metadata.get('agent_delivery') == 'pending':
                                metadata['agent_delivery'] = 'failed'
                                db.execute('UPDATE voice SET metadata=? WHERE id=?', (json.dumps(metadata), vid))
                        with contextlib.suppress(Exception):
                            await self.changed('speech.auto_submit_failed', jid)
        except asyncio.CancelledError:
            # User cancellation has already changed status; shutdown becomes interrupted/failed.
            with self.store.connect() as db:
                if db.execute("UPDATE speech_jobs SET status='failed',error='전사가 중단되었습니다. 수동으로 재시도하세요.',updated_at=? WHERE id=? AND status='running'", (utcnow(),jid)).rowcount:
                    db.execute("UPDATE voice SET status='transcription_failed' WHERE id=?", (vid,))
            if asyncio.current_task().cancelling():
                raise
        except Exception as exc:
            message = (str(exc) if isinstance(exc, TranscriptionError) else
                       'OpenAI 전사 요청을 완료하지 못했습니다. Odyssey의 API 키·예산·네트워크 상태를 확인하세요. PC/로컬 Whisper로 대체하지 않습니다.'
                       if self.profile.pc_free else '전사 실행 오류입니다. 설치 상태와 음성 파일을 확인하세요.')
            with self.store.connect() as db:
                if db.execute("UPDATE speech_jobs SET status='failed',error=?,elapsed=?,updated_at=? WHERE id=? AND status='running'", (message,time.monotonic()-start,utcnow(),jid)).rowcount:
                    db.execute("UPDATE voice SET status='transcription_failed' WHERE id=?", (vid,))
            await self.changed('speech.failed', jid)
        finally:
            self.active_id = self.active_cancel = None
