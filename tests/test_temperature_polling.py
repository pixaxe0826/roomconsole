"""Synthetic LAN-API contract tests. Never contact the user's 192.168 network."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import indoor_temperature as module
from app.indoor_temperature import TemperatureError, TemperaturePoller, TemperatureStore, source_url_validated, validate_http_reading
from app.main import create_app

URL = "http://192.168.0.22/api/temperature"
ID = "room-temp-01"
START = 1792929600.0


def sample(**changes):
    return {"temperature_c": 26.8, "voltage_v": .2680, "adc_raw": 100.1, **changes}


@pytest.fixture
def source(tmp_path):
    clock = [START]
    service = TemperatureStore(tmp_path, clock=lambda: clock[0])
    service.set_source(ID, URL)
    return service, service.polling_sources()[0], clock


def poll(service, source, handler):
    async def run():
        worker = TemperaturePoller(service, transport=httpx.MockTransport(handler))
        async with worker.new_client() as client:
            await worker.poll_source(client, source)
    asyncio.run(run())


@pytest.mark.parametrize("url", [URL, "http://10.1.2.3:8080/api/temperature", "http://172.16.0.8/api/temperature"])
def test_lan_ipv4_url_allowed(url):
    assert source_url_validated(url) == url
    assert source_url_validated("http://192.168.0.22:80/api/temperature") == URL


@pytest.mark.parametrize("url", [
    "https://192.168.0.22/api/temperature", "http://localhost/api/temperature",
    "http://127.0.0.1/api/temperature", "http://169.254.169.254/api/temperature",
    "http://8.8.8.8/api/temperature", "http://192.168.0.22.evil.test/api/temperature",
    "http://user:pass@192.168.0.22/api/temperature", "http://[::1]/api/temperature",
    "http://192.168.0.22/api/temperature?target=x", "http://192.168.0.22/api/temperature#x",
    "http://192.168.0.22:0/api/temperature", "http://192.168.0.22:65536/api/temperature",
    "http://192.168.0.22/other", " http://192.168.0.22/api/temperature",
])
def test_source_cannot_be_arbitrary_outbound_request(url):
    with pytest.raises(ValueError):
        source_url_validated(url)


@pytest.mark.parametrize("changes", [
    {"temperature_c": True}, {"temperature_c": "26.80"}, {"temperature_c": None},
    {"temperature_c": float("nan")}, {"temperature_c": float("inf")}, {"temperature_c": 126},
    {"voltage_v": "0.2680"}, {"voltage_v": -1}, {"voltage_v": 101},
    {"adc_raw": False}, {"adc_raw": -1}, {"adc_raw": 65536}, {"adc_raw": 10**400},
])
def test_invalid_source_numbers(changes):
    with pytest.raises(TemperatureError):
        validate_http_reading(sample(**changes))


def test_fractional_adc_extra_fields_and_zero():
    assert validate_http_reading(sample(uptime=1234)) == sample()
    assert validate_http_reading(sample(temperature_c=0, voltage_v=0, adc_raw=0)) == sample(temperature_c=0, voltage_v=0, adc_raw=0)
    for body in ([], {}, {"temperature_c": 26.8}):
        with pytest.raises(TemperatureError):
            validate_http_reading(body)


def test_poll_uses_api_temperature_without_voltage_conversion(source):
    service, config, clock = source
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=sample(temperature_c=22.1))
    poll(service, config, handler)
    state = service.snapshot(ID)
    assert state["temperature_c"] == 22.1 and state["voltage_v"] == .268 and state["adc_raw"] == 100.1
    assert state["calibrated"] is None and state["acquisition"] == "http_pull" and state["source_status"] == "ok"
    assert state["received_at"] == state["last_attempt_at"] and state["status"] == "ok"
    assert "source_url" not in state and "revision" not in state
    assert len(requests) == 1 and str(requests[0].url) == URL and requests[0].method == "GET"
    assert "authorization" not in requests[0].headers and "cookie" not in requests[0].headers
    assert requests[0].headers["accept-encoding"] == "identity"
    clock[0] += 10
    poll(service, config, handler)
    assert service.snapshot(ID)["received_at"] != state["received_at"]  # Repeated physical value can be a new successful response.


@pytest.mark.parametrize("response,code", [
    (lambda: httpx.Response(503, text="offline"), "http_status"),
    (lambda: httpx.Response(302, headers={"location": "http://8.8.8.8/"}), "http_status"),
    (lambda: httpx.Response(200, content="not JSON"), "invalid_json"),
    (lambda: httpx.Response(200, json={"temperature_c": 26.8}), "invalid_payload"),
    (lambda: httpx.Response(200, content=b" " * 2049), "response_too_large"),
])
def test_failure_never_refreshes_last_good_reading(source, response, code):
    service, config, clock = source
    service.record_polled(ID, config["revision"], sample())
    good = service.snapshot(ID)
    clock[0] += 10
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return response()
    poll(service, config, handler)
    failed = service.snapshot(ID)
    assert failed["source_status"] == "error" and failed["source_error"] == code
    assert failed["received_at"] == good["received_at"] and failed["temperature_c"] == 26.8
    assert failed["adc_raw"] == 100.1 and failed["age_seconds"] == 10
    assert calls == [URL]  # Redirect responses are not followed.
    clock[0] = START + 30
    assert service.snapshot(ID)["status"] == "stale"
    clock[0] = START + 120
    assert service.snapshot(ID)["status"] == "offline"
    poll(service, config, lambda _: httpx.Response(200, json=sample(temperature_c=27)))
    recovered = service.snapshot(ID)
    assert recovered["source_error"] is None and recovered["status"] == "ok" and recovered["temperature_c"] == 27


def test_timeout_and_connection_error_are_recorded_without_fake_zero(source, monkeypatch):
    service, config, _ = source
    monkeypatch.setattr(module, "HTTP_TOTAL_SECONDS", .02)
    async def slow(request):
        await asyncio.sleep(.2)
        return httpx.Response(200, json=sample())
    poll(service, config, slow)
    first = service.snapshot(ID)
    assert first["source_error"] == "timeout" and first["temperature_c"] is None and first["received_at"] is None
    def disconnected(request):
        raise httpx.ConnectError("synthetic", request=request)
    poll(service, config, disconnected)
    assert service.snapshot(ID)["source_error"] == "connection"


def test_stream_limit_stops_consuming_and_closes_response(source):
    service, config, _ = source
    class Chunks(httpx.AsyncByteStream):
        seen = 0
        closed = False
        async def __aiter__(self):
            for _ in range(20):
                self.seen += 1
                yield b" " * 1024
        async def aclose(self):
            self.closed = True
    stream = Chunks()
    poll(service, config, lambda _: httpx.Response(200, stream=stream))
    assert stream.seen == 3 and stream.closed
    assert service.snapshot(ID)["source_error"] == "response_too_large"


def test_config_updates_reject_late_old_responses_and_preserve_history(source):
    service, old, clock = source
    service.record_polled(ID, old["revision"], sample())
    previous = service.snapshot(ID)
    service.set_source(ID, URL, "Same room renamed")
    assert service.polling_sources()[0]["revision"] == old["revision"]
    assert service.snapshot(ID)["received_at"] == previous["received_at"]
    service.set_source(ID, "http://192.168.0.23/api/temperature")
    current = service.polling_sources()[0]
    assert current["revision"] != old["revision"]
    assert service.snapshot(ID)["temperature_c"] is None
    clock[0] += 10
    with pytest.raises(TemperatureError) as stale:
        service.record_polled(ID, old["revision"], sample(temperature_c=99))
    assert stale.value.code == 409
    service.record_poll_failure(ID, old["revision"], "connection")
    assert service.snapshot(ID)["source_error"] is None
    with service.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 1
    service.record_polled(ID, current["revision"], sample(temperature_c=27))
    assert service.snapshot(ID)["temperature_c"] == 27
    assert service.snapshot(ID)["summary"] == {"min_c": 27, "max_c": 27}
    assert all(point["temperature_c"] == 27 for point in service.snapshot(ID)["history"])
    service.revoke(ID)
    assert service.polling_sources() == []
    with pytest.raises(TemperatureError):
        service.record_polled(ID, current["revision"], sample())


def test_http_mode_blocks_legacy_sender_token(source):
    service, _, _ = source
    token = service.create_device("old-sender", "Old sender")["token"]
    service.set_source("old-sender", URL)
    body = {"device_id": "old-sender", "sample_id": "old-1", "temperature_c": 99, "sensor_ok": True, "calibrated": True}
    with pytest.raises(TemperatureError) as wrong_mode:
        service.ingest(token, body)
    assert wrong_mode.value.code == 401
    with pytest.raises(ValueError):
        service.create_device("old-sender", "Old sender", rotate=True)
    visible = json.dumps(service.list_devices()) + json.dumps(service.snapshot("old-sender"))
    assert token not in visible and hashlib.sha256(token.encode()).hexdigest() not in visible


def test_installed_v1_database_migrates_without_losing_measurements(tmp_path):
    path = tmp_path / "indoor-temperature.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE temperature_devices(device_id TEXT PRIMARY KEY,label TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,received_at REAL,temperature_c REAL,sensor_ok INTEGER,calibrated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE temperature_samples(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL REFERENCES temperature_devices(device_id),sample_id TEXT NOT NULL,digest TEXT NOT NULL,received_at REAL NOT NULL,temperature_c REAL,sensor_ok INTEGER NOT NULL,calibrated INTEGER NOT NULL,UNIQUE(device_id,sample_id));
        """)
        db.execute("INSERT INTO temperature_devices VALUES(?,?,?,1,?,?,?,1,1)", (ID, "Synthetic v1", "synthetic-digest", START, START, 25.2))
        db.execute("INSERT INTO temperature_samples(device_id,sample_id,digest,received_at,temperature_c,sensor_ok,calibrated) VALUES(?,?,?,?,?,1,1)", (ID, "v1-sample", "synthetic", START, 25.2))
    service = TemperatureStore(tmp_path, clock=lambda: START)
    state = service.snapshot(ID)
    assert state["temperature_c"] == 25.2 and state["calibrated"] is True and state["acquisition"] == "push"
    assert state["adc_raw"] is None and state["history"][0]["temperature_c"] == 25.2
    service.set_source(ID, URL)
    assert service.snapshot(ID)["status"] == "waiting"
    reopened = TemperatureStore(tmp_path, clock=lambda: START + 10)
    current = reopened.polling_sources()[0]
    reopened.record_polled(ID, current["revision"], sample())
    assert reopened.snapshot(ID)["summary"] == {"min_c": 26.8, "max_c": 26.8}
    with reopened.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 2


def test_source_configuration_cli_has_no_credentials(tmp_path):
    command = [sys.executable, "scripts/configure_temperature_sensor.py", "--data-dir", str(tmp_path)]
    result = subprocess.run(command + ["set-source", "--url", URL], capture_output=True, text=True, check=True)
    body = json.loads(result.stdout)
    assert body["source_url"] == URL and body["device_id"] == ID and "token" not in body
    listing = json.loads(subprocess.check_output(command + ["list"], text=True))
    assert listing["devices"][0]["acquisition"] == "http_pull"
    assert listing["devices"][0]["source_status"] == "waiting"


def test_background_polling_starts_without_a_widget_request_and_cancels(tmp_path):
    app = create_app(tmp_path, weather_enabled=False)
    service = app.state.indoor_temperature
    service.set_source(ID, URL)
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=sample())
    app.state.indoor_temperature_poller.transport = httpx.MockTransport(handler)
    app.state.indoor_temperature_poller.interval = .05
    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        while service.snapshot(ID)["temperature_c"] is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert service.snapshot(ID)["temperature_c"] == 26.8
        client.headers["Authorization"] = "Bearer " + app.state.admin_token
        response = client.get("/api/indoor-temperature/" + ID)
        assert response.json()["adc_raw"] == 100.1 and response.headers["cache-control"] == "no-store"
    count = len(requests)
    time.sleep(.1)
    assert len(requests) == count and count >= 1


def test_no_configured_source_performs_no_http(tmp_path):
    app = create_app(tmp_path, weather_enabled=False)
    def forbidden(request):
        raise AssertionError("Unconfigured app must not make outbound sensor calls")
    app.state.indoor_temperature_poller.transport = httpx.MockTransport(forbidden)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_poll_lifespan_preserves_existing_context_and_state(tmp_path):
    from fastapi import FastAPI
    events = []
    @asynccontextmanager
    async def original(app):
        events.append("start")
        yield {"existing": "preserved"}
        events.append("stop")
    app = FastAPI(lifespan=original)
    module.register_routes(app, tmp_path, None, lambda: {}, lambda: {})
    with TestClient(app) as client:
        assert client.app_state == {"existing": "preserved"}
    assert events == ["start", "stop"]
