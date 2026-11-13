"""Synthetic telemetry/auth tests; no actual MCU, ADC, LAN, or real readings."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.indoor_temperature import TemperatureError, TemperatureStore, validate_reading
from app.main import create_app

START = datetime.fromisoformat("2026-10-25T12:00:00+00:00").timestamp()


def reading(**changes):
    return {"device_id": "room-temp-01", "sample_id": "synthetic-1", "temperature_c": 26.8, "sensor_ok": True, "calibrated": False, **changes}


@pytest.fixture
def sensor(tmp_path):
    clock = [START]
    service = TemperatureStore(tmp_path, clock=lambda: clock[0])
    token = service.create_device("room-temp-01", "Synthetic room")["token"]
    return service, token, clock


@pytest.fixture
def hub(tmp_path):
    app = create_app(tmp_path / "hub", weather_enabled=False)
    clock = [START]
    app.state.indoor_temperature.clock = lambda: clock[0]
    token = app.state.indoor_temperature.create_device("room-temp-01", "Synthetic room")["token"]
    with TestClient(app) as admin:
        display = TestClient(app)
        device = TestClient(app)
        admin.headers.update({"Authorization": "Bearer " + app.state.admin_token, "X-Room-Request": "1"})
        display.headers["X-Room-Request"] = "1"
        pair = admin.post("/api/devices/pair", json={"name": "Synthetic iPad"}).json()
        assert display.post("/api/devices/claim", json={"code": pair["path"].split("=")[1]}).status_code == 200
        device.headers["Authorization"] = "Bearer " + token
        yield app, admin, display, device, token, clock, pair["device_id"]
        display.close()
        device.close()


def place(app, device_id="room-temp-01"):
    layout = app.state.store.get("layout")
    layout["widgets"][0].update(type="indoor-temperature", config={"device_id": device_id, "location": "Synthetic room"})
    app.state.store.set("layout", layout)


def test_initial_registration_and_restart(sensor):
    service, token, clock = sensor
    snapshot = service.snapshot("room-temp-01")
    assert snapshot["registered"] and snapshot["status"] == "waiting"
    assert snapshot["temperature_c"] is None and snapshot["received_at"] is None
    assert service.snapshot("unknown")["registered"] is False
    service.ingest(token, reading(temperature_c=0))
    reopened = TemperatureStore(service.path.parent, clock=lambda: clock[0])
    result = reopened.snapshot("room-temp-01")
    assert result["temperature_c"] == 0 and result["status"] == "ok"


@pytest.mark.parametrize("value", [True, False, "26.8", "", None, [], {}, float("nan"), float("inf"), float("-inf"), -40.001, 125.001, 10**400])
def test_invalid_numeric_readings(value):
    with pytest.raises(TemperatureError):
        validate_reading(reading(temperature_c=value))


@pytest.mark.parametrize("changes", [{"device_id": "../bad"}, {"device_id": "A"}, {"sample_id": "bad/path"}, {"sample_id": "x" * 65}, {"sensor_ok": 1}, {"calibrated": "false"}, {"sensor_ok": False}, {"sensor_ok": False, "temperature_c": None, "calibrated": True}, {"extra": 1}])
def test_invalid_contract(changes):
    with pytest.raises(TemperatureError):
        validate_reading(reading(**changes))


def test_fresh_stale_offline_error_and_recovery(sensor):
    service, token, clock = sensor
    service.ingest(token, reading())
    for age, status in [(0, "ok"), (29.9, "ok"), (30, "stale"), (119.9, "stale"), (120, "offline")]:
        clock[0] = START + age
        result = service.snapshot("room-temp-01")
        assert result["status"] == status and result["temperature_c"] == 26.8
    service.ingest(token, reading(sample_id="synthetic-2", temperature_c=None, sensor_ok=False))
    error = service.snapshot("room-temp-01")
    assert error["status"] == "sensor_error" and error["temperature_c"] is None
    clock[0] += 10
    service.ingest(token, reading(sample_id="synthetic-3", temperature_c=27.1, calibrated=True))
    assert service.snapshot("room-temp-01")["calibrated"] is True


def test_duplicate_never_refreshes_age_and_conflict_rejected(sensor):
    service, token, clock = sensor
    original = service.ingest(token, reading())
    clock[0] += 180
    duplicate = service.ingest(token, reading())
    assert duplicate["duplicate"] and duplicate["received_at"] == original["received_at"]
    assert service.snapshot("room-temp-01")["status"] == "offline"
    with pytest.raises(TemperatureError) as conflict:
        service.ingest(token, reading(temperature_c=24))
    assert conflict.value.code == 409


def test_rate_limit_persists_and_clock_reversal_is_not_fresh(sensor):
    service, token, clock = sensor
    service.ingest(token, reading())
    reopened = TemperatureStore(service.path.parent, clock=lambda: clock[0])
    with pytest.raises(TemperatureError) as rate:
        reopened.ingest(token, reading(sample_id="synthetic-2"))
    assert rate.value.code == 429 and rate.value.retry_after == 5
    clock[0] -= 60
    assert reopened.snapshot("room-temp-01")["status"] == "stale"
    with pytest.raises(TemperatureError) as backwards:
        reopened.ingest(token, reading(sample_id="synthetic-2"))
    assert backwards.value.code == 503


def test_concurrent_duplicate_only_inserts_once(sensor):
    service, token, _ = sensor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.ingest(token, reading()), range(8)))
    assert sum(not result["duplicate"] for result in results) == 1
    with service.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 1


def test_device_tokens_cannot_cross_sensors_rotate_revoke(sensor):
    service, token, clock = sensor
    other = service.create_device("room-temp-02", "Synthetic room 2")["token"]
    with pytest.raises(TemperatureError) as rejected:
        service.ingest(token, reading(device_id="room-temp-02"))
    assert rejected.value.code == 401
    service.ingest(other, reading(device_id="room-temp-02"))
    fresh = service.create_device("room-temp-01", "Synthetic room", rotate=True)["token"]
    with pytest.raises(TemperatureError):
        service.ingest(token, reading())
    service.ingest(fresh, reading())
    service.revoke("room-temp-01")
    clock[0] += 10
    with pytest.raises(TemperatureError):
        service.ingest(fresh, reading(sample_id="new"))
    assert service.snapshot("room-temp-01")["temperature_c"] is None


def test_history_bounded_null_gaps_and_retention(sensor):
    service, token, clock = sensor
    for i in range(65):
        clock[0] = START + i * 60
        service.ingest(token, reading(sample_id=f"minute-{i}", temperature_c=None if i == 62 else 20 + i / 100, sensor_ok=i != 62))
    snapshot = service.snapshot("room-temp-01")
    assert len(snapshot["history"]) == 61
    assert any(p["temperature_c"] is None for p in snapshot["history"])
    assert snapshot["summary"]["max_c"] == 20.64
    clock[0] += 86410
    service.ingest(token, reading(sample_id="next-day"))
    with service.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM temperature_samples").fetchone()[0] == 1


def test_connections_close_on_success_and_exception(sensor):
    service, _, _ = sensor
    with service.connection() as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with service.connection() as failed:
            raise RuntimeError("synthetic failure")
    with pytest.raises(sqlite3.ProgrammingError):
        failed.execute("SELECT 1")


def test_symlink_database_rejected(tmp_path):
    target = tmp_path / "original"
    target.write_bytes(b"synthetic")
    try:
        (tmp_path / "indoor-temperature.sqlite3").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        TemperatureStore(tmp_path)


def test_http_device_only_ingest_and_display_opt_in(hub):
    app, admin, display, device, _, _, _ = hub
    path = "/api/indoor-temperature/room-temp-01"
    assert display.get(path).status_code == 403
    assert admin.get(path).status_code == 200
    assert device.get("/api/state").status_code == 401
    assert device.get(path).status_code == 401
    assert device.post("/api/indoor-temperature/readings", json=reading()).status_code == 200
    place(app)
    response = display.get(path)
    assert response.status_code == 200 and response.json()["temperature_c"] == 26.8
    assert response.headers["cache-control"] == "no-store"
    assert admin.post("/api/indoor-temperature/readings", json=reading()).status_code == 401
    assert display.post("/api/indoor-temperature/readings", json=reading()).status_code == 401
    assert device.post("/api/voice/text", json={"request_id": "synthetic", "source": "sensor", "text": "test"}).status_code == 401


def test_http_secrets_not_in_snapshot_and_display_revoked(hub):
    app, admin, display, device, token, _, display_id = hub
    place(app)
    device.post("/api/indoor-temperature/readings", json=reading())
    visible = display.get("/api/state").text + display.get("/api/indoor-temperature/room-temp-01").text + json.dumps(app.state.indoor_temperature.list_devices())
    assert token not in visible and hashlib.sha256(token.encode()).hexdigest() not in visible
    assert admin.delete("/api/devices/" + display_id).status_code == 200
    assert display.get("/api/indoor-temperature/room-temp-01").status_code == 401


def test_http_body_type_limit_and_origin(hub):
    _, _, _, device, _, _, _ = hub
    path = "/api/indoor-temperature/readings"
    assert device.post(path, content="x", headers={"content-type": "text/plain"}).status_code == 415
    assert device.post(path, content=" " * 2049, headers={"content-type": "application/json"}).status_code == 413
    assert device.post(path, content="{", headers={"content-type": "application/json"}).status_code == 422
    assert device.post(path, content=json.dumps(reading(temperature_c=float("nan"))), headers={"content-type": "application/json"}).status_code == 422
    assert device.post(path, json=reading(), headers={"origin": "https://other.invalid"}).status_code == 403


def test_layout_removal_and_other_sensor_hidden(hub):
    app, admin, display, _, _, _, _ = hub
    place(app, "room-temp-02")
    assert display.get("/api/indoor-temperature/room-temp-01").status_code == 403
    layout = app.state.store.get("layout")
    layout["widgets"][0]["type"] = "clock"
    app.state.store.set("layout", layout)
    assert display.get("/api/indoor-temperature/room-temp-02").status_code == 403


def test_widget_discovered_without_changing_saved_layout(hub):
    app, admin, _, _, _, _, _ = hub
    state = admin.get("/api/state").json()
    widget = next(w for w in state["widgets"] if w["id"] == "indoor-temperature")
    assert widget["defaultSize"] == {"w": 2, "h": 2}
    assert all(w["type"] != "indoor-temperature" for w in state["layout"]["widgets"])


def test_only_public_firmware_files_are_publishable():
    from scripts.repo_policy import source_path_allowed
    directory = "firmware/room_temperature_esp8266/"
    for name in ("room_temperature_esp8266.ino", "config.example.h", "README_KO.md"):
        assert source_path_allowed(directory + name)
    for name in ("config.h", "private.h", "wifi.txt", "config.example.h.bak"):
        assert not source_path_allowed(directory + name)
