"""The load generator: its rotation, its switch, its summary, and the real versions answering it
through the local function URL runner."""

import datetime
import json
import sys
import threading

import pytest

import traffic
from conftest import HERE, NOW

ROOT = HERE.parents[2]
STATES = ROOT / "demo-states" / "orders-service"
SOCKS = "/api/price?product=Sports%20socks%2C%203%20pairs"


class FakeClock:
    """Time that moves only when the generator sleeps."""

    def __init__(self, now=NOW):
        self.value = now
        self.lock = threading.Lock()

    def now(self):
        return self.value

    def sleep(self, seconds):
        with self.lock:
            self.value += seconds


class FakeSsm:
    def __init__(self, values):
        self.values, self.asked = values, []

    def get_parameter(self, Name, WithDecryption=False):
        self.asked.append(Name)
        return {"Parameter": {"Value": self.values[Name.removeprefix(traffic.SSM_PREFIX)]}}


@pytest.fixture(autouse=True)
def no_cached_settings(monkeypatch):
    monkeypatch.setattr(traffic, "_loki", None)
    monkeypatch.setenv("TARGET_URL", "https://orders.example/")


def test_the_rotation_is_12_orders_2_pages_and_each_product_once():
    assert len(traffic.ROTATION) == 20
    assert traffic.ROTATION.count("/api/orders") == 12 and traffic.ROTATION.count("/") == 2
    prices = [path for path in traffic.ROTATION if path.startswith("/api/price")]
    assert prices == [traffic.price(product) for product in traffic.PRODUCTS]


def test_the_products_are_the_six_in_handler_py():
    namespace = {}
    source = (STATES / "1.27.0" / "handler.py").read_text()
    start = source.index("PRODUCTS = [")
    exec(source[start:source.index("]\n", start) + 2], namespace)
    assert traffic.PRODUCTS == [name for name, _ in namespace["PRODUCTS"]]


def test_any_sixty_seconds_hold_exactly_three_of_the_failing_path():
    for start in range(1_793_000_000, 1_793_000_060):
        paths = [traffic.path_at(second) for second in range(start, start + 60)]
        assert paths.count(SOCKS) == 3 and paths.count("/api/orders") == 36 and paths.count("/") == 6


@pytest.mark.parametrize("value, now, on", [
    ("off", NOW, False),
    ("unset", NOW, False),
    ("", NOW, False),
    (None, NOW, False),
    ("2026-11-07T09:00:00+00:00", NOW, True),
    ("2026-11-07T09:00:00+00:00", datetime.datetime(2026, 11, 7, 9, 0, 1, tzinfo=datetime.timezone.utc).timestamp(), False),
    ("2026-11-07T09:00:00Z", NOW, True),
])
def test_the_switch_is_off_unless_it_names_a_time_still_to_come(value, now, on):
    assert traffic.traffic_on(value, now) is on


def test_off_exits_at_once_and_reads_only_the_switch():
    ssm = FakeSsm({traffic.SWITCH: "off"})
    sent = []
    assert traffic.handler({}, None, ssm=ssm, now=lambda: NOW, get=lambda url, timeout: sent.append(url)) == {"sent": 0}
    assert ssm.asked == ["/release-demo/error-budget/traffic-until"] and sent == []


def test_a_run_sends_sixty_one_a_second_with_a_five_second_timeout():
    clock = FakeClock()
    calls, slept = [], []

    def answer(url, timeout):
        calls.append((url, timeout))
        return 200

    def sleep(seconds):
        slept.append(seconds)
        clock.sleep(seconds)

    results = traffic.send_all("https://orders.example", now=clock.now, sleep=sleep, get=answer)
    assert len(results) == 60 and len(calls) == 60
    assert all(timeout == 5.0 for _, timeout in calls)
    first = int(NOW) + 1
    assert [path for path, _ in results] == [traffic.path_at(second) for second in range(first, first + 60)]
    assert slept[0] == pytest.approx(first - NOW) and all(seconds == pytest.approx(1) for seconds in slept[1:])
    assert clock.now() == pytest.approx(first + 59) and sum(1 for url, _ in calls if url.endswith(SOCKS)) == 3


def test_the_last_requests_keep_time_for_the_summary_inside_the_functions_65_seconds():
    clock = FakeClock()
    started = clock.now()
    timeouts = []

    def answer(url, timeout):
        timeouts.append(timeout)
        return 200

    traffic.send_all("https://orders.example", now=clock.now, sleep=clock.sleep, get=answer,
                     remaining=lambda: 65 - (clock.now() - started))
    assert max(timeouts) == 5.0 and min(timeouts) <= 3.5


def test_the_summary_counts_5xx_and_no_answer_as_failed_and_404_not():
    summary = traffic.summarize([("/api/orders", 200), (SOCKS, 502), (SOCKS, 502), ("/", 0), ("/api/price?product=Rain%20jacket", 404)])
    assert summary == {"requests": 5, "failed": 3, "by_status": {"200": 1, "502": 2, "0": 1, "404": 1},
                       "failed_paths": {"/api/price?product=Sports socks, 3 pairs": 2, "/": 1}}


def test_a_run_pushes_one_line_with_the_labels(monkeypatch):
    clock = FakeClock()
    pushed = []
    ssm = FakeSsm({traffic.SWITCH: "2026-11-07T09:00:00+00:00", "grafana/logs-url": "https://logs.example/",
                   "grafana/logs-user": "123", "grafana/logs-token": "test-token, not a real one"})

    class Reply:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=None, context=None):
        pushed.append((request.full_url, json.loads(request.data), request.get_header("Authorization"), timeout))
        return Reply()

    monkeypatch.setattr(traffic.urllib.request, "urlopen", urlopen)
    reply = traffic.handler({}, None, ssm=ssm, now=clock.now, sleep=clock.sleep,
                            get=lambda url, timeout: 502 if url.endswith(SOCKS) else 200)
    assert reply == {"sent": 60, "failed": 3}
    url, body, auth, _ = pushed[0]
    assert len(pushed) == 1 and url == "https://logs.example/loki/api/v1/push" and auth.startswith("Basic ")
    stream = body["streams"][0]
    assert stream["stream"] == {"source": "release-demo-loadgen", "service": "orders-service", "environment": "production",
                                "kind": "traffic"}
    line = json.loads(stream["values"][0][1])
    assert line == {"requests": 60, "failed": 3, "by_status": {"200": 57, "502": 3},
                    "failed_paths": {"/api/price?product=Sports socks, 3 pairs": 3}}


def test_a_summary_that_cant_be_sent_stops_nothing_and_forgets_the_settings(monkeypatch, capsys):
    ssm = FakeSsm({"grafana/logs-url": "https://logs.example", "grafana/logs-user": "1", "grafana/logs-token": "test-token"})

    def refuse(*args, **kwargs):
        raise OSError("no route")

    monkeypatch.setattr(traffic.urllib.request, "urlopen", refuse)
    assert traffic.push(ssm, {"requests": 60}) == 0
    assert traffic._loki is None
    out = capsys.readouterr().out
    assert "not sent" in out and "test-token" not in out


def serving(state):
    """A local function URL for one demo state, as scripts/lambda_local.py serves it."""
    sys.path.insert(1, str(ROOT / "scripts"))
    import lambda_local

    function = lambda_local.load_handler(STATES / state, "handler.handler")
    server = lambda_local.make_server(function, "orders-service-production", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.mark.parametrize("state, failed", [("1.26.0", 0), ("1.27.0", 3), ("1.27.1", 0), ("1.28.0", 3), ("1.28.1", 0), ("1.4.0", 0)])
def test_one_minute_against_each_real_version(state, failed, monkeypatch):
    server = serving(state)
    clock = FakeClock()
    one_at_a_time = threading.Lock()  # a whole minute with no real seconds between: the local runner's backlog is 5

    def get(url, timeout):
        with one_at_a_time:
            return traffic.fetch(url, timeout)

    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        results = traffic.send_all(base, now=clock.now, sleep=clock.sleep, get=get)
    finally:
        server.shutdown()
        server.server_close()
    summary = traffic.summarize(results)
    assert summary["requests"] == 60 and summary["failed"] == failed
    if failed:
        assert summary["failed_paths"] == {"/api/price?product=Sports socks, 3 pairs": 3}
        assert summary["by_status"]["502"] == 3
    if state == "1.4.0":
        assert summary["by_status"] == {"200": 42, "404": 18}  # the older page has no /api/price: not a failure
