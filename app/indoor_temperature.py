"""Shared indoor sensor polling and legacy telemetry; separate SQLite, display opt-in.

HTTP temperature, humidity, pressure and dust raw values are used as supplied.
Freshness describes a validated HTTP response, not the sensor's sampling clock.
"""
import asyncio
from contextlib import asynccontextmanager, closing, contextmanager, suppress
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

DEVICE_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SAMPLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
STALE_SECONDS = 30
OFFLINE_SECONDS = 120
MIN_INTERVAL = 5
RETENTION_SECONDS = 86400
MAX_DEVICES = 16
MAX_BODY = 2048
POLL_SECONDS = 10
HTTP_TIMEOUT_SECONDS = 3
HTTP_TOTAL_SECONDS = 4
DEFAULT_SOURCE_URL = "http://192.168.0.22/api/sensors"
SOURCE_PATHS = {"/api/sensors": "environment", "/api/temperature": "temperature"}
LEGACY_FIELDS = ("temperature_c", "voltage_v", "adc_raw")
ENVIRONMENT_FIELDS = ("temperature_c", "humidity_percent", "pressure_hpa", "dust_adc_raw", "dust_a0_voltage_v", "dust_sensor_voltage_v")
READING_FIELDS = LEGACY_FIELDS + ENVIRONMENT_FIELDS[1:]
# Broad input sanity limits, not sensor specifications or air-quality thresholds.
FIELD_RANGES = {"temperature_c": (-40, 125), "humidity_percent": (0, 100),
                "pressure_hpa": (0, 2000), "voltage_v": (0, 100), "adc_raw": (0, 65535),
                "dust_adc_raw": (0, 65535), "dust_a0_voltage_v": (0, 100), "dust_sensor_voltage_v": (0, 100)}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
logger = logging.getLogger(__name__)


class TemperatureError(ValueError):
    def __init__(self, code, detail, retry_after=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def device_id_valid(value):
    return isinstance(value, str) and DEVICE_RE.fullmatch(value) is not None


def source_url_validated(value):
    """Only explicit LAN IPv4 and the sensor path; no DNS, redirects or secrets."""
    message = "센서 URL은 http://사설IPv4[:포트]/api/sensors 형식이어야 합니다. 이전 /api/temperature도 지원합니다."
    if not isinstance(value, str) or len(value) > 200 or any(ord(c) <= 32 for c in value):
        raise ValueError(message)
    try:
        parts = urlsplit(value)
        host = ipaddress.IPv4Address(parts.hostname or "")
        port = 80 if parts.port is None else parts.port
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if (parts.scheme != "http" or parts.username is not None or parts.password is not None
            or parts.path not in SOURCE_PATHS or parts.query or parts.fragment
            or not any(host in network for network in PRIVATE_NETWORKS) or not 1 <= port <= 65535):
        raise ValueError(message)
    return "http://" + str(host) + (":" + str(port) if port != 80 else "") + parts.path


def source_kind_for_url(url):
    return SOURCE_PATHS.get(urlsplit(url).path)


def validate_http_reading(payload, source_kind="temperature"):
    """Validate one complete response. ADC averages stay fractional; extra fields are ignored."""
    if source_kind not in {"environment", "temperature"}:
        raise TemperatureError(422, "센서 응답 형식을 확인하세요.")
    fields = ENVIRONMENT_FIELDS if source_kind == "environment" else LEGACY_FIELDS
    if not isinstance(payload, dict) or not set(fields).issubset(payload):
        raise TemperatureError(422, "센서 응답에 다음 필드가 필요합니다: " + ", ".join(fields))
    values = {}
    for key in fields:
        value = payload[key]
        if type(value) not in (int, float):
            raise TemperatureError(422, "센서 응답 값은 유한한 숫자여야 합니다.")
        try:
            value = float(value)
        except (OverflowError, ValueError):
            raise TemperatureError(422, "센서 응답 숫자 범위를 확인하세요.") from None
        if not math.isfinite(value):
            raise TemperatureError(422, "센서 응답 값은 유한한 숫자여야 합니다.")
        minimum, maximum = FIELD_RANGES[key]
        if not minimum <= value <= maximum or (key == "pressure_hpa" and value == 0):
            raise TemperatureError(422, "센서 응답 숫자 범위를 확인하세요.")
        values[key] = value
    return values


def validate_reading(payload):
    fields = {"device_id", "sample_id", "temperature_c", "sensor_ok", "calibrated"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise TemperatureError(422, "온도 요청의 필드를 확인하세요.")
    if not device_id_valid(payload["device_id"]):
        raise TemperatureError(422, "센서 ID 형식이 올바르지 않습니다.")
    if not isinstance(payload["sample_id"], str) or not SAMPLE_RE.fullmatch(payload["sample_id"]):
        raise TemperatureError(422, "측정 ID 형식이 올바르지 않습니다.")
    if type(payload["sensor_ok"]) is not bool or type(payload["calibrated"]) is not bool:
        raise TemperatureError(422, "측정 상태는 boolean이어야 합니다.")
    value = payload["temperature_c"]
    if payload["sensor_ok"]:
        if type(value) not in (int, float):
            raise TemperatureError(422, "온도는 유효한 숫자여야 합니다.")
        try:
            value = float(value)
        except (OverflowError, ValueError):
            raise TemperatureError(422, "온도 범위를 확인하세요.") from None
        if not math.isfinite(value) or not -40 <= value <= 125:
            raise TemperatureError(422, "온도 허용 범위는 -40~125°C입니다.")
    elif value is not None or payload["calibrated"]:
        raise TemperatureError(422, "측정 오류는 null 온도와 미보정 상태로 보내세요.")
    return {**payload, "temperature_c": value}


class TemperatureStore:
    """Connections close explicitly, including failed transactions."""

    def __init__(self, data_dir, clock=None):
        self.clock = clock or time.time
        self.path = Path(data_dir) / "indoor-temperature.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_paths()
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS temperature_devices (
                    device_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL, received_at REAL,
                    temperature_c REAL, sensor_ok INTEGER, calibrated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS temperature_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL REFERENCES temperature_devices(device_id),
                    sample_id TEXT NOT NULL, digest TEXT NOT NULL,
                    received_at REAL NOT NULL, temperature_c REAL,
                    sensor_ok INTEGER NOT NULL, calibrated INTEGER NOT NULL,
                    UNIQUE(device_id, sample_id)
                );
                CREATE INDEX IF NOT EXISTS temperature_samples_time
                    ON temperature_samples(received_at);
                CREATE INDEX IF NOT EXISTS temperature_samples_device_time
                    ON temperature_samples(device_id, received_at);
                CREATE TABLE IF NOT EXISTS temperature_sources (
                    device_id TEXT PRIMARY KEY REFERENCES temperature_devices(device_id),
                    url TEXT NOT NULL, revision TEXT NOT NULL,
                    last_attempt REAL, last_error TEXT
                );
            """)
            # Additive migration also accepts installed v1/v2 databases.
            db.execute("BEGIN IMMEDIATE")
            for table in ("temperature_devices", "temperature_samples"):
                columns = {row["name"] for row in db.execute("PRAGMA table_info(" + table + ")")}
                for column in READING_FIELDS[1:]:
                    if column not in columns:
                        db.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " REAL")
                if table == "temperature_samples" and "source_revision" not in columns:
                    db.execute("ALTER TABLE temperature_samples ADD COLUMN source_revision TEXT")

    def _check_paths(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("온도 저장소 경로가 일반 파일이 아닙니다.")

    @contextmanager
    def connection(self):
        self._check_paths()
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def create_device(self, device_id, label, *, rotate=False):
        if not device_id_valid(device_id):
            raise ValueError("device-id는 소문자로 시작하는 영문/숫자/_/- 1~64자입니다.")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or any(ord(c) < 32 for c in label):
            raise ValueError("label은 제어문자 없는 1~40자여야 합니다.")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM temperature_sources WHERE device_id=?", (device_id,)).fetchone():
                raise ValueError("API 조회 센서는 set-source 명령으로 설정하세요.")
            existing = db.execute("SELECT device_id FROM temperature_devices WHERE device_id=?", (device_id,)).fetchone()
            if existing and not rotate:
                raise ValueError("이미 등록된 센서입니다. 토큰 교체는 rotate 명령을 사용하세요.")
            if rotate and not existing:
                raise ValueError("등록되지 않은 센서입니다.")
            if existing:
                db.execute("UPDATE temperature_devices SET token_hash=?, enabled=1, label=? WHERE device_id=?", (digest, label.strip(), device_id))
            else:
                if db.execute("SELECT COUNT(*) FROM temperature_devices").fetchone()[0] >= MAX_DEVICES:
                    raise ValueError("센서는 최대 16개입니다.")
                db.execute("INSERT INTO temperature_devices(device_id,label,token_hash,created_at) VALUES(?,?,?,?)", (device_id, label.strip(), digest, self.clock()))
        return {"device_id": device_id, "label": label.strip(), "token": token}

    def revoke(self, device_id):
        if not device_id_valid(device_id):
            raise ValueError("센서 ID를 확인하세요.")
        with self.connection() as db:
            if not db.execute("UPDATE temperature_devices SET enabled=0 WHERE device_id=?", (device_id,)).rowcount:
                raise ValueError("등록되지 않은 센서입니다.")

    def list_devices(self):
        with self.connection() as db:
            rows = db.execute("""SELECT d.device_id,d.label,d.enabled,d.received_at,s.url,s.last_attempt,s.last_error
                FROM temperature_devices d LEFT JOIN temperature_sources s ON s.device_id=d.device_id
                ORDER BY d.device_id""").fetchall()
        return [{"device_id": r["device_id"], "label": r["label"], "enabled": bool(r["enabled"]),
                 "received_at": iso(r["received_at"]) if r["received_at"] is not None else None,
                 "acquisition": "http_pull" if r["url"] else "push", "source_url": r["url"],
                 "source_kind": source_kind_for_url(r["url"]) if r["url"] else "temperature",
                 "last_attempt_at": iso(r["last_attempt"]) if r["last_attempt"] is not None else None,
                 "source_error": r["last_error"],
                 "source_status": ("error" if r["last_error"] else "waiting" if r["last_attempt"] is None else "ok") if r["url"] else None}
                for r in rows]

    def set_source(self, device_id, url, label="내 방"):
        if not device_id_valid(device_id):
            raise ValueError("센서 ID 형식을 확인하세요.")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or any(ord(c) < 32 for c in label):
            raise ValueError("label은 제어문자 없는 1~40자여야 합니다.")
        url = source_url_validated(url)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            device = db.execute("SELECT enabled FROM temperature_devices WHERE device_id=?", (device_id,)).fetchone()
            previous = db.execute("SELECT url FROM temperature_sources WHERE device_id=?", (device_id,)).fetchone()
            if device is None:
                if db.execute("SELECT COUNT(*) FROM temperature_devices").fetchone()[0] >= MAX_DEVICES:
                    raise ValueError("센서는 최대 16개입니다.")
                # A pull source has no issued sender credential.
                token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                db.execute("INSERT INTO temperature_devices(device_id,label,token_hash,created_at) VALUES(?,?,?,?)", (device_id, label.strip(), token_hash, self.clock()))
            else:
                db.execute("UPDATE temperature_devices SET enabled=1,label=? WHERE device_id=?", (label.strip(), device_id))
            if not previous or previous["url"] != url or (device and not device["enabled"]):
                db.execute("""INSERT INTO temperature_sources(device_id,url,revision) VALUES(?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET url=excluded.url,revision=excluded.revision,last_attempt=NULL,last_error=NULL""", (device_id, url, secrets.token_hex(16)))
                cleared = ",".join(field + "=NULL" for field in READING_FIELDS)
                db.execute("UPDATE temperature_devices SET received_at=NULL,sensor_ok=NULL,calibrated=0," + cleared + " WHERE device_id=?", (device_id,))
        return {"device_id": device_id, "label": label.strip(), "acquisition": "http_pull", "source_kind": source_kind_for_url(url), "source_url": url, "poll_seconds": POLL_SECONDS}

    def polling_sources(self):
        with self.connection() as db:
            rows = db.execute("SELECT s.device_id,s.url,s.revision FROM temperature_sources s JOIN temperature_devices d ON d.device_id=s.device_id WHERE d.enabled=1 ORDER BY s.device_id").fetchall()
        return [dict(row) for row in rows]

    def record_poll_failure(self, device_id, revision, code):
        if code not in {"timeout", "connection", "http_status", "invalid_json", "invalid_payload", "response_too_large", "encoded_response", "clock", "source_config"}:
            raise ValueError("Unknown polling failure code")
        with self.connection() as db:
            # A late response from an old URL/configuration cannot change new state.
            db.execute("UPDATE temperature_sources SET last_attempt=?,last_error=? WHERE device_id=? AND revision=? AND EXISTS(SELECT 1 FROM temperature_devices d WHERE d.device_id=temperature_sources.device_id AND d.enabled=1)", (self.clock(), code, device_id, revision))

    def record_polled(self, device_id, revision, payload):
        now = self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT d.received_at,s.url FROM temperature_devices d JOIN temperature_sources s ON s.device_id=d.device_id WHERE d.device_id=? AND d.enabled=1 AND s.revision=?", (device_id, revision)).fetchone()
            if row is None:
                raise TemperatureError(409, "수집 중 센서 설정이 바뀌었습니다.")
            values = validate_http_reading(payload, source_kind_for_url(row["url"]))
            self._check_interval(row["received_at"], now)
            sample_id = "http:" + secrets.token_hex(16)
            digest = hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()
            readings = tuple(values.get(field) for field in READING_FIELDS)
            columns = ",".join(READING_FIELDS)
            placeholders = ",".join("?" for _ in READING_FIELDS)
            db.execute("INSERT INTO temperature_samples(device_id,sample_id,digest,received_at,sensor_ok,calibrated,source_revision," + columns + ") VALUES(?,?,?,?,1,0,?," + placeholders + ")", (device_id, sample_id, digest, now, revision, *readings))
            assignments = ",".join(field + "=?" for field in READING_FIELDS)
            db.execute("UPDATE temperature_devices SET received_at=?,sensor_ok=1,calibrated=0," + assignments + " WHERE device_id=?", (now, *readings, device_id))
            db.execute("UPDATE temperature_sources SET last_attempt=?,last_error=NULL WHERE device_id=? AND revision=?", (now, device_id, revision))
            db.execute("DELETE FROM temperature_samples WHERE received_at<?", (now - RETENTION_SECONDS,))
        return {"ok": True, "received_at": iso(now)}

    @staticmethod
    def _check_interval(received_at, now):
        if received_at is None:
            return
        elapsed = now - received_at
        if elapsed < -1:
            raise TemperatureError(503, "서버 시각을 확인하세요.", retry_after=10)
        if elapsed < MIN_INTERVAL:
            raise TemperatureError(429, "센서 전송 간격은 최소 5초입니다.", retry_after=max(1, math.ceil(MIN_INTERVAL - elapsed)))

    def ingest(self, token, payload):
        if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
            raise TemperatureError(401, "센서 전용 인증이 필요합니다.")
        body = validate_reading(payload)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        now = self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            device = db.execute("SELECT * FROM temperature_devices WHERE device_id=? AND enabled=1", (body["device_id"],)).fetchone()
            if not device or not hmac.compare_digest(device["token_hash"], token_hash):
                raise TemperatureError(401, "센서 전용 인증이 필요합니다.")
            if db.execute("SELECT 1 FROM temperature_sources WHERE device_id=?", (body["device_id"],)).fetchone():
                raise TemperatureError(401, "API 조회 센서에는 직접 전송할 수 없습니다.")
            old = db.execute("SELECT digest,received_at FROM temperature_samples WHERE device_id=? AND sample_id=?", (body["device_id"], body["sample_id"])).fetchone()
            if old:
                if not hmac.compare_digest(old["digest"], digest):
                    raise TemperatureError(409, "같은 측정 ID에 다른 값이 들어왔습니다.")
                return {"ok": True, "duplicate": True, "received_at": iso(old["received_at"])}
            self._check_interval(device["received_at"], now)
            db.execute("INSERT INTO temperature_samples(device_id,sample_id,digest,received_at,temperature_c,sensor_ok,calibrated) VALUES(?,?,?,?,?,?,?)", (body["device_id"], body["sample_id"], digest, now, body["temperature_c"], int(body["sensor_ok"]), int(body["calibrated"])))
            db.execute("UPDATE temperature_devices SET received_at=?,temperature_c=?,sensor_ok=?,calibrated=? WHERE device_id=?", (now, body["temperature_c"], int(body["sensor_ok"]), int(body["calibrated"]), body["device_id"]))
            db.execute("DELETE FROM temperature_samples WHERE received_at<?", (now - RETENTION_SECONDS,))
        return {"ok": True, "duplicate": False, "received_at": iso(now)}

    def snapshot(self, device_id):
        return self._snapshot(device_id, air=False)

    def air_snapshot(self, device_id):
        return self._snapshot(device_id, air=True)

    def _snapshot(self, device_id, *, air):
        if not device_id_valid(device_id):
            raise TemperatureError(422, "센서 ID를 확인하세요.")
        now = self.clock()
        fields = ENVIRONMENT_FIELDS[3:] if air else LEGACY_FIELDS + ENVIRONMENT_FIELDS[1:3]
        summary_fields = {"min_adc_raw": ("MIN", "dust_adc_raw"), "max_adc_raw": ("MAX", "dust_adc_raw")} if air else {"min_c": ("MIN", "temperature_c"), "max_c": ("MAX", "temperature_c")}
        result = {"device_id": device_id, "registered": False, "status": "waiting", "sensor_ok": None,
                  "acquisition": "push", "source_kind": None, "source_status": None, "source_error": None,
                  "last_attempt_at": None, "received_at": None, "server_time": iso(now), "age_seconds": None,
                  "stale_after_seconds": STALE_SECONDS, "offline_after_seconds": OFFLINE_SECONDS,
                  "history": [], "summary": {key: None for key in summary_fields}, "history_bucket_seconds": 60,
                  **dict.fromkeys(fields)}
        if not air:
            result["calibrated"] = False
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT received_at,sensor_ok,calibrated," + ",".join(fields) + " FROM temperature_devices WHERE device_id=? AND enabled=1", (device_id,)).fetchone()
            if not row:
                return result
            result["registered"] = True
            source = db.execute("SELECT url,last_attempt,last_error,revision FROM temperature_sources WHERE device_id=?", (device_id,)).fetchone()
            result["source_kind"] = source_kind_for_url(source["url"]) if source else "temperature"
            if source:
                result.update(acquisition="http_pull",
                              source_status="error" if source["last_error"] else "waiting" if source["last_attempt"] is None else "ok",
                              source_error=source["last_error"], last_attempt_at=iso(source["last_attempt"]) if source["last_attempt"] is not None else None)
                if not air:
                    result["calibrated"] = None
            if not air and result["source_kind"] == "environment":
                for field in ("humidity_percent", "pressure_hpa"):
                    summary_fields["min_" + field] = ("MIN", field)
                    summary_fields["max_" + field] = ("MAX", field)
                result["summary"] = dict.fromkeys(summary_fields)
            # Legacy temperature ADC/voltage are not dust readings.
            if row["received_at"] is None or (air and result["source_kind"] != "environment"):
                return result
            revision = source["revision"] if source else None
            points = db.execute("SELECT received_at," + ",".join(fields) + """ FROM temperature_samples
                WHERE id IN (SELECT MAX(id) FROM temperature_samples
                    WHERE device_id=? AND received_at>=? AND received_at<=?
                    AND (? IS NULL OR source_revision=?)
                    GROUP BY CAST(received_at/60 AS INTEGER))
                ORDER BY received_at LIMIT 61""", (device_id, now - 3600, now, revision, revision)).fetchall()
            aggregates = ",".join(operation + "(" + field + ") AS " + key for key, (operation, field) in summary_fields.items())
            summary = db.execute("SELECT " + aggregates + " FROM temperature_samples WHERE device_id=? AND received_at>=? AND received_at<=? AND sensor_ok=1 AND (? IS NULL OR source_revision=?)", (device_id, now - 3600, now, revision, revision)).fetchone()
        age = max(0., now - row["received_at"])
        status = "offline" if age >= OFFLINE_SECONDS else "stale" if age >= STALE_SECONDS or row["received_at"] > now + 1 else "ok" if row["sensor_ok"] else "sensor_error"
        result.update(status=status, sensor_ok=bool(row["sensor_ok"]), received_at=iso(row["received_at"]),
                      age_seconds=round(age, 3), **{field: row[field] for field in fields},
                      history=[{"at": iso(p["received_at"]), **{field: p[field] for field in fields}} for p in points],
                      summary=dict(summary))
        if not air:
            result["calibrated"] = None if source else bool(row["calibrated"])
        return result


class PollFailure(ValueError):
    pass


class TemperaturePoller:
    """One background worker per application lifespan; no browser-origin requests."""

    def __init__(self, service, *, transport=None, interval=POLL_SECONDS):
        self.service = service
        self.transport = transport
        self.interval = interval

    def new_client(self):
        import httpx
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
                                 transport=self.transport, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                                 headers={"Accept": "application/json", "Accept-Encoding": "identity", "Cache-Control": "no-cache"})

    async def fetch(self, client, url):
        try:
            url = source_url_validated(url)
        except ValueError:
            raise PollFailure("source_config") from None
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise PollFailure("http_status")
            if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise PollFailure("encoded_response")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > MAX_BODY:
                    raise PollFailure("response_too_large")
                raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise PollFailure("invalid_json") from None
        try:
            return validate_http_reading(payload, source_kind_for_url(url))
        except TemperatureError:
            raise PollFailure("invalid_payload") from None

    async def poll_source(self, client, source):
        import httpx
        error = None
        try:
            payload = await asyncio.wait_for(self.fetch(client, source["url"]), timeout=HTTP_TOTAL_SECONDS)
            await asyncio.to_thread(self.service.record_polled, source["device_id"], source["revision"], payload)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            error = "timeout"
        except httpx.HTTPError:
            error = "connection"
        except PollFailure as exc:
            error = str(exc)
        except TemperatureError as exc:
            if exc.code in {409, 429}:
                return  # Configuration changed, or another worker already recorded this cycle.
            error = "clock" if exc.code == 503 else "invalid_payload"
        if error:
            await asyncio.to_thread(self.service.record_poll_failure, source["device_id"], source["revision"], error)

    async def poll_once(self, client, sources=None):
        if sources is None:
            sources = await asyncio.to_thread(self.service.polling_sources)
        semaphore = asyncio.Semaphore(4)

        async def one(source):
            async with semaphore:
                try:
                    await self.poll_source(client, source)
                except (sqlite3.Error, OSError, ValueError):
                    logger.warning("실내 센서 저장소 작업을 완료하지 못했습니다.")
        await asyncio.gather(*(one(source) for source in sources))

    async def run(self):
        client = None
        try:
            while True:
                started = asyncio.get_running_loop().time()
                try:
                    sources = await asyncio.to_thread(self.service.polling_sources)
                    if sources:
                        if client is None:
                            client = self.new_client()
                        await self.poll_once(client, sources)
                except (sqlite3.Error, OSError, ValueError):
                    logger.warning("실내 센서 수집 설정을 읽지 못했습니다.")
                await asyncio.sleep(max(.05, self.interval - (asyncio.get_running_loop().time() - started)))
        finally:
            if client is not None:
                await client.aclose()


def widget_allows(store, device_id, widget_type="indoor-temperature"):
    # Explicit close also protects installations whose legacy Store has no FD hotfix.
    with closing(store.connect()) as db:
        row = db.execute("SELECT value FROM kv WHERE key='layout'").fetchone()
    layout = json.loads(row["value"]) if row else {}
    for widget in layout.get("widgets", []):
        config = widget.get("config") or {}
        if widget.get("type") == widget_type and config.get("device_id", "room-temp-01") == device_id:
            return True
    return False


def register_routes(app, data, store, admin, viewer):
    from fastapi import Depends, HTTPException, Request
    service = TemperatureStore(data)
    app.state.indoor_temperature = service
    poller = TemperaturePoller(service)
    app.state.indoor_temperature_poller = poller
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_temperature(application):
        async with previous_lifespan(application) as state:
            worker = asyncio.create_task(poller.run(), name="indoor-temperature-poller")
            try:
                yield state
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    app.router.lifespan_context = lifespan_with_temperature

    def translate(exc):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return HTTPException(exc.code, exc.detail, headers=headers)

    @app.post("/api/indoor-temperature/readings")
    async def receive(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "센서 전용 인증이 필요합니다.")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "application/json 형식이 필요합니다.")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY:
                raise HTTPException(413, "온도 요청은 최대 2KiB입니다.")
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise HTTPException(422, "JSON 형식이 올바르지 않습니다.") from None
        try:
            return await asyncio.to_thread(service.ingest, auth[7:], payload)
        except TemperatureError as exc:
            raise translate(exc) from None
        except sqlite3.Error:
            raise HTTPException(503, "온도 저장소를 사용할 수 없습니다.") from None

    @app.get("/api/indoor-temperature/{device_id}")
    async def display(device_id: str, actor=Depends(viewer)):
        if not device_id_valid(device_id):
            raise HTTPException(422, "센서 ID를 확인하세요.")
        try:
            if actor["role"] != "admin" and not await asyncio.to_thread(widget_allows, store, device_id):
                raise HTTPException(403, "이 센서의 실내 온도 위젯을 먼저 배치하세요.")
            return await asyncio.to_thread(service.snapshot, device_id)
        except TemperatureError as exc:
            raise translate(exc) from None
        except sqlite3.Error:
            raise HTTPException(503, "온도 저장소를 사용할 수 없습니다.") from None

    @app.get("/api/indoor-air-quality/{device_id}")
    async def air_display(device_id: str, actor=Depends(viewer)):
        if not device_id_valid(device_id):
            raise HTTPException(422, "센서 ID를 확인하세요.")
        try:
            if actor["role"] != "admin" and not await asyncio.to_thread(widget_allows, store, device_id, "indoor-air-quality"):
                raise HTTPException(403, "이 센서의 실내 공기 질 위젯을 먼저 배치하세요.")
            return await asyncio.to_thread(service.air_snapshot, device_id)
        except TemperatureError as exc:
            raise translate(exc) from None
        except sqlite3.Error:
            raise HTTPException(503, "센서 저장소를 사용할 수 없습니다.") from None
