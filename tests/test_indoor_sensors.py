"""Synthetic six-field sensor and display-permission tests; no physical LAN calls."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.indoor_temperature import (DEFAULT_SOURCE_URL, TemperatureError, TemperaturePoller,
                                    TemperatureStore, source_url_validated, validate_http_reading)
from app.main import create_app

ID = "room-temp-01"
URL = "http://192.168.0.22/api/sensors"
START = 1792972800.0
FIELDS = ("temperature_c", "humidity_percent", "pressure_hpa", "dust_adc_raw", "dust_a0_voltage_v", "dust_sensor_voltage_v")


def sample(**changes):
    return {"temperature_c": 26.78, "humidity_percent": 40.13, "pressure_hpa": 1009.09,
            "dust_adc_raw": 26.0, "dust_a0_voltage_v": .0697, "dust_sensor_voltage_v": .2922, **changes}


@pytest.fixture
def sensors(tmp_path):
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


def test_new_default_and_narrow_both_paths():
    assert DEFAULT_SOURCE_URL == URL
    for url in (URL, "http://10.2.3.4:8080/api/sensors", "http://172.16.0.1/api/sensors",
                "http://192.168.0.22/api/temperature"):
        assert source_url_validated(url) == url
    for url in (URL + "/", URL + "?x=1", URL + "#x", "http://192.168.0.22/api/../api/sensors",
                "http://192.168.0.22/api/%73ensors", "https://192.168.0.22/api/sensors",
                "http://127.0.0.1/api/sensors", "http://8.8.8.8/api/sensors"):
        with pytest.raises(ValueError):
            source_url_validated(url)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("invalid", [None, True, "26.0", float("nan"), float("inf")])
def test_six_field_values_must_be_real_finite_numbers(field, invalid):
    with pytest.raises(TemperatureError):
        validate_http_reading(sample(**{field: invalid}), "environment")


@pytest.mark.parametrize("field", FIELDS)
def test_every_field_is_required_as_one_response(field):
    body = sample()
    body.pop(field)
    with pytest.raises(TemperatureError):
        validate_http_reading(body, "environment")


@pytest.mark.parametrize("changes", [
    {"temperature_c": -41}, {"temperature_c": 126}, {"humidity_percent": -.1}, {"humidity_percent": 100.1},
    {"pressure_hpa": 0}, {"pressure_hpa": 2001}, {"dust_adc_raw": -1}, {"dust_adc_raw": 65536},
    {"dust_a0_voltage_v": -.01}, {"dust_sensor_voltage_v": 101}, {"pressure_hpa": 10**400},
])
def test_input_sanity_limits_are_not_quality_grades(changes):
    with pytest.raises(TemperatureError):
        validate_http_reading(sample(**changes), "environment")


def test_zero_fraction_and_extra_fields_are_not_reinterpreted():
    body = sample(temperature_c=0, humidity_percent=0, dust_adc_raw=0, dust_a0_voltage_v=0, dust_sensor_voltage_v=0)
    assert validate_http_reading({**body, "future": "ignored"}, "environment") == body
    fractional = sample(dust_adc_raw=26.375)
    assert validate_http_reading(fractional, "environment") == fractional
    with pytest.raises(TemperatureError):
        validate_http_reading({"temperature_c": 26.78, "voltage_v": .268, "adc_raw": 100.1}, "environment")


def test_one_response_produces_two_projections_with_exact_fields(sensors):
    service, config, _ = sensors
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=sample(dust_adc_raw=26.375))
    poll(service, config, handler)
    temperature = service.snapshot(ID)
    air = service.air_snapshot(ID)
    assert len(requests) == 1 and str(requests[0].url) == URL
    assert requests[0].method == "GET" and "authorization" not in requests[0].headers
    assert temperature["temperature_c"] == 26.78
    assert temperature["humidity_percent"] == 40.13 and temperature["pressure_hpa"] == 1009.09
    assert air["dust_adc_raw"] == 26.375 and air["dust_a0_voltage_v"] == .0697 and air["dust_sensor_voltage_v"] == .2922
    for key in ("status", "source_kind", "acquisition", "source_status", "received_at", "last_attempt_at", "age_seconds"):
        assert temperature[key] == air[key]
    assert air["source_kind"] == "environment" and air["status"] == "ok"
    assert temperature["voltage_v"] is None and temperature["adc_raw"] is None
    assert "dust_adc_raw" not in temperature and "temperature_c" not in air
    for visible in (temperature, air):
        assert "source_url" not in visible and "revision" not in visible and "token" not in visible
        assert not any(key in visible for key in ("pm25", "pm10", "aqi", "quality_grade"))
    with service.connection() as db:
        row = db.execute("SELECT * FROM temperature_samples").fetchone()
        assert all(row[field] == sample(dust_adc_raw=26.375)[field] for field in FIELDS)
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["missing_dust", "invalid_humidity", "bad_json", "timeout", "http_error"])
def test_failed_whole_response_keeps_both_last_good_values_and_age(sensors, failure):
    service, config, clock = sensors
    service.record_polled(ID, config["revision"], sample())
    before_temp, before_air = service.snapshot(ID), service.air_snapshot(ID)
    clock[0] += 10
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "http_error":
            return httpx.Response(503)
        if failure == "bad_json":
            return httpx.Response(200, content=b"{")
        body = sample(temperature_c=30, dust_adc_raw=77)
        if failure == "missing_dust":
            body.pop("dust_adc_raw")
        else:
            body["humidity_percent"] = None
        return httpx.Response(200, json=body)
    poll(service, config, handler)
    for getter, before in ((service.snapshot, before_temp), (service.air_snapshot, before_air)):
        after = getter(ID)
        assert after["received_at"] == before["received_at"] and after["history"] == before["history"]
        assert after["source_status"] == "error" and after["age_seconds"] == 10
        assert after["last_attempt_at"] != after["received_at"]
    assert service.snapshot(ID)["temperature_c"] == 26.78 and service.air_snapshot(ID)["dust_adc_raw"] == 26
    for age, expected in ((30, "stale"), (120, "offline")):
        clock[0] = START + age
        assert service.snapshot(ID)["status"] == expected and service.air_snapshot(ID)["status"] == expected
    poll(service, config, lambda _: httpx.Response(200, json=sample(humidity_percent=45, dust_adc_raw=30.5)))
    assert service.snapshot(ID)["humidity_percent"] == 45 and service.air_snapshot(ID)["dust_adc_raw"] == 30.5
    assert service.air_snapshot(ID)["source_error"] is None


def test_first_invalid_response_never_becomes_zero(sensors):
    service, config, _ = sensors
    poll(service, config, lambda _: httpx.Response(200, json={"temperature_c": 26.78}))
    assert service.snapshot(ID)["temperature_c"] is None
    air = service.air_snapshot(ID)
    assert air["dust_adc_raw"] is None and air["received_at"] is None and air["history"] == []
    assert air["source_status"] == "error"


def test_history_and_extremes_use_corresponding_metrics_without_conversion(sensors):
    service, config, clock = sensors
    service.record_polled(ID, config["revision"], sample(temperature_c=20, humidity_percent=30, pressure_hpa=995, dust_adc_raw=99.5))
    clock[0] += 10
    service.record_polled(ID, config["revision"], sample(temperature_c=25, humidity_percent=50, pressure_hpa=1010, dust_adc_raw=1.25))
    clock[0] += 60
    service.record_polled(ID, config["revision"], sample())
    temp, air = service.snapshot(ID), service.air_snapshot(ID)
    assert temp["summary"] == {"min_c": 20, "max_c": 26.78, "min_humidity_percent": 30, "max_humidity_percent": 50,
                                "min_pressure_hpa": 995, "max_pressure_hpa": 1010}
    assert air["summary"] == {"min_adc_raw": 1.25, "max_adc_raw": 99.5}
    assert len(air["history"]) == 2 and len(temp["history"]) == 2
    assert air["history"][0]["dust_adc_raw"] == 1.25  # last sample in minute, while true extrema include every sample
    assert temp["history"][0]["humidity_percent"] == 50
    assert all("temperature_c" not in point for point in air["history"])
    assert all("dust_adc_raw" not in point for point in temp["history"])
    clock[0] += 86410
    service.record_polled(ID, config["revision"], sample())
    with service.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 1


def test_source_change_clears_all_views_and_ignores_old_inflight_response(sensors):
    service, old, clock = sensors
    service.record_polled(ID, old["revision"], sample())
    service.set_source(ID, URL, "Renamed room")
    assert service.polling_sources()[0]["revision"] == old["revision"]
    service.set_source(ID, "http://192.168.0.23/api/sensors")
    current = service.polling_sources()[0]
    for getter in (service.snapshot, service.air_snapshot):
        state = getter(ID)
        assert state["received_at"] is None and state["history"] == []
    assert service.snapshot(ID)["humidity_percent"] is None and service.air_snapshot(ID)["dust_sensor_voltage_v"] is None
    with pytest.raises(TemperatureError) as error:
        service.record_polled(ID, old["revision"], sample(dust_adc_raw=900))
    assert error.value.code == 409
    service.record_poll_failure(ID, old["revision"], "connection")
    assert service.air_snapshot(ID)["source_error"] is None
    clock[0] += 10
    service.record_polled(ID, current["revision"], sample(dust_adc_raw=1))
    assert service.air_snapshot(ID)["summary"] == {"min_adc_raw": 1, "max_adc_raw": 1}
    service.revoke(ID)
    assert service.polling_sources() == [] and service.air_snapshot(ID)["registered"] is False


def test_legacy_adc_is_never_mislabelled_as_air_quality(tmp_path):
    service = TemperatureStore(tmp_path, clock=lambda: START)
    service.set_source(ID, "http://192.168.0.22/api/temperature")
    config = service.polling_sources()[0]
    service.record_polled(ID, config["revision"], {"temperature_c": 20, "voltage_v": .2, "adc_raw": 100.1})
    air = service.air_snapshot(ID)
    assert air["source_kind"] == "temperature" and air["registered"]
    assert air["dust_adc_raw"] is None and air["received_at"] is None and air["history"] == []
    service.set_source(ID, URL)
    config = service.polling_sources()[0]
    service.record_polled(ID, config["revision"], sample())
    assert service.air_snapshot(ID)["source_kind"] == "environment" and service.air_snapshot(ID)["dust_adc_raw"] == 26
    service.set_source(ID, "http://192.168.0.22/api/temperature")
    assert service.air_snapshot(ID)["dust_adc_raw"] is None


def test_existing_v2_database_adds_fields_and_keeps_original_data(tmp_path):
    path = tmp_path / "indoor-temperature.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE temperature_devices(device_id TEXT PRIMARY KEY,label TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,received_at REAL,temperature_c REAL,sensor_ok INTEGER,calibrated INTEGER NOT NULL DEFAULT 0,voltage_v REAL,adc_raw REAL);
        CREATE TABLE temperature_samples(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL REFERENCES temperature_devices(device_id),sample_id TEXT NOT NULL,digest TEXT NOT NULL,received_at REAL NOT NULL,temperature_c REAL,sensor_ok INTEGER NOT NULL,calibrated INTEGER NOT NULL,voltage_v REAL,adc_raw REAL,source_revision TEXT,UNIQUE(device_id,sample_id));
        CREATE TABLE temperature_sources(device_id TEXT PRIMARY KEY REFERENCES temperature_devices(device_id),url TEXT NOT NULL,revision TEXT NOT NULL,last_attempt REAL,last_error TEXT);
        """)
        db.execute("INSERT INTO temperature_devices VALUES(?,?,?,1,?,?,?,1,0,?,?)", (ID, "Synthetic v2", "synthetic-token-hash", START, START, 25, .25, 88.8))
        db.execute("INSERT INTO temperature_sources VALUES(?,?,?,?,NULL)", (ID, "http://192.168.0.22/api/temperature", "v2-revision", START))
        db.execute("INSERT INTO temperature_samples VALUES(1,?,?,?,?,?,1,0,?,?,?)", (ID, "v2-sample", "synthetic", START, 25, .25, 88.8, "v2-revision"))
    service = TemperatureStore(tmp_path, clock=lambda: START + 10)
    assert service.snapshot(ID)["adc_raw"] == 88.8 and service.snapshot(ID)["humidity_percent"] is None
    assert service.air_snapshot(ID)["dust_adc_raw"] is None
    service.set_source(ID, URL)
    config = service.polling_sources()[0]
    service.record_polled(ID, config["revision"], sample())
    reopened = TemperatureStore(tmp_path, clock=lambda: START + 10)
    assert reopened.snapshot(ID)["humidity_percent"] == 40.13 and reopened.air_snapshot(ID)["dust_adc_raw"] == 26
    with reopened.connection() as db:
        old = db.execute("SELECT * FROM temperature_samples WHERE id=1").fetchone()
        assert old["adc_raw"] == 88.8 and old["dust_adc_raw"] is None
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 2


def test_cli_default_configures_one_shared_source(tmp_path):
    command = [sys.executable, "scripts/configure_temperature_sensor.py", "--data-dir", str(tmp_path)]
    result = json.loads(subprocess.check_output(command + ["set-source"], text=True))
    assert result["source_url"] == URL and result["source_kind"] == "environment" and "token" not in result
    listing = json.loads(subprocess.check_output(command + ["list"], text=True))
    assert len(listing["devices"]) == 1 and listing["devices"][0]["device_id"] == ID


@contextmanager
def hub_with_source(tmp_path):
    app = create_app(tmp_path, weather_enabled=False)
    service = app.state.indoor_temperature
    service.set_source(ID, URL)
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=sample())
    app.state.indoor_temperature_poller.transport = httpx.MockTransport(handler)
    with TestClient(app) as admin:
        admin.headers.update({"Authorization": "Bearer " + app.state.admin_token, "X-Room-Request": "1"})
        # No second lifespan: both HTTP sessions use this application's one worker.
        display = TestClient(app)
        try:
            display.headers["X-Room-Request"] = "1"
            pairing = admin.post("/api/devices/pair", json={"name": "Synthetic sensors display"}).json()
            assert display.post("/api/devices/claim", json={"code": pairing["path"].split("=")[1]}).status_code == 200
            deadline = time.monotonic() + 2
            while service.snapshot(ID)["received_at"] is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert service.snapshot(ID)["received_at"] is not None
            yield app, admin, display, calls, pairing["device_id"]
        finally:
            display.close()


def place(app, kind, sensor_id=ID):
    layout = app.state.store.get("layout")
    layout["widgets"][0].update(type=kind, config={"device_id": sensor_id, "location": "Synthetic room"})
    app.state.store.set("layout", layout)


def test_each_display_projection_requires_its_own_widget_and_revokes(tmp_path):
    with hub_with_source(tmp_path) as (app, admin, display, _, display_id):
        temperature_path = "/api/indoor-temperature/" + ID
        air_path = "/api/indoor-air-quality/" + ID
        assert display.get(temperature_path).status_code == 403 and display.get(air_path).status_code == 403
        assert admin.get(air_path).json()["dust_adc_raw"] == 26
        place(app, "indoor-temperature")
        assert display.get(temperature_path).status_code == 200 and display.get(air_path).status_code == 403
        place(app, "indoor-air-quality")
        air = display.get(air_path)
        assert air.status_code == 200 and air.headers["cache-control"] == "no-store"
        assert "temperature_c" not in air.json() and "source_url" not in air.json()
        assert display.get(temperature_path).status_code == 403
        place(app, "indoor-air-quality", "room-temp-02")
        assert display.get(air_path).status_code == 403
        place(app, "indoor-air-quality")
        assert admin.delete("/api/devices/" + display_id).status_code == 200
        assert display.get(air_path).status_code == 401


def test_both_widget_reads_share_one_background_acquisition(tmp_path):
    with hub_with_source(tmp_path) as (app, admin, _, calls, _):
        before = len(calls)
        for _ in range(3):
            assert admin.get("/api/indoor-temperature/" + ID).json()["humidity_percent"] == 40.13
            assert admin.get("/api/indoor-air-quality/" + ID).json()["dust_sensor_voltage_v"] == .2922
        assert before == 1 and len(calls) == before
        with app.state.indoor_temperature.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 1
        state = admin.get("/api/state").json()
        for kind in ("indoor-temperature", "indoor-air-quality"):
            manifest = next(item for item in state["widgets"] if item["id"] == kind)
            assert manifest["defaultSize"] == {"w": 2, "h": 2}
            assert all(item["type"] != kind for item in state["layout"]["widgets"])
