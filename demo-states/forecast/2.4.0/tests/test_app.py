import json
import signal
import threading
import urllib.request

import app
from conftest import as_json


def get(path):
    return as_json(app.respond(path))


def test_health_reports_service_version_environment_and_pod():
    status, body = get("/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["service"] == "forecast"
    assert body["version"] == app.VERSION
    assert body["environment"] == "production"
    assert body["pod"] == "forecast-7d9f8b6c5-abcde"


def test_ready_once_the_forecast_loads():
    status, body = get("/ready")
    assert status == 200
    assert body["ready"] is True
    assert body["reason"] == "4 weeks"


def test_not_ready_when_the_forecast_doesnt_load(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "HERE", tmp_path)
    status, body = get("/ready")
    assert status == 503
    assert body["ready"] is False
    assert get("/health")[0] == 200  # alive all the same: the liveness probe mustn't restart it


def test_the_whole_forecast():
    status, body = get("/api/forecast")
    assert status == 200
    assert body["count"] == 4
    assert body["weeks"][0]["units"]["Water bottle"] == 950


def test_one_week():
    body = get("/api/forecast?week=2026-W48")[1]
    assert [row["week"] for row in body["weeks"]] == ["2026-W48"]


def test_one_product():
    body = get("/api/forecast?product=Rain%20jacket")[1]
    assert body["count"] == 4
    assert all(list(row["units"]) == ["Rain jacket"] for row in body["weeks"])


def test_anything_else_is_not_found():
    assert get("/nothing")[0] == 404


def test_commit_comes_from_the_build(tmp_path):
    (tmp_path / "VERSION").write_text("9.9.9\n")
    assert app.read_package(tmp_path) == ("9.9.9", "local")
    (tmp_path / "build_info.json").write_text('{"commit": "abc"}')
    assert app.read_package(tmp_path) == ("9.9.9", "abc")


def test_the_server_answers_over_http_and_stops_on_sigterm(monkeypatch):
    """main() as the container runs it, on a free port, and a real GET."""
    started = threading.Event()
    servers = []

    class Server(app.ThreadingHTTPServer):
        def __init__(self, address, handler):
            super().__init__(("127.0.0.1", 0), handler)
            servers.append(self)

        def serve_forever(self, *args, **kwargs):
            started.set()
            super().serve_forever(poll_interval=0.01)

    handlers = {}
    monkeypatch.setattr(app, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(app.signal, "signal", lambda number, handler: handlers.update({number: handler}))
    runner = threading.Thread(target=app.main, daemon=True)
    runner.start()
    assert started.wait(5)
    port = servers[0].server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as reply:
        assert json.loads(reply.read())["service"] == "forecast"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/forecast?week=2026-W46", timeout=5) as reply:
        assert json.loads(reply.read())["count"] == 1
    handlers[signal.SIGTERM]()
    runner.join(5)
    assert not runner.is_alive()
    servers[0].server_close()
