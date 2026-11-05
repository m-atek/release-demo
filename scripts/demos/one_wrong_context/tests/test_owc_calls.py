"""The caller: real sockets where a socket is the point, a fake clock where time is."""

import http.server
import io
import json
import re
import socket
import threading
from pathlib import Path

import pytest
import yaml

import calls

ROOT = Path(__file__).resolve().parents[4]
ANSI = re.compile(r"\033\[[0-9;]*m")


class Handler(http.server.BaseHTTPRequestHandler):
    status, body = 200, b'{"count": 1, "products": [{"name": "x"}]}'

    def do_GET(self):
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *_):
        pass


@pytest.fixture
def server():
    made = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("Answers", (Handler,), {}))
    thread = threading.Thread(target=made.serve_forever, daemon=True)
    thread.start()
    yield made
    made.shutdown()
    made.server_close()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_port_and_route_are_catalogs_in_production():
    config = yaml.safe_load((ROOT / "services.yaml").read_text())["catalog"]
    assert calls.PORT == config["environments"]["production"]["node_port"]
    assert calls.ROUTE in config["environments"]["production"]["check"]["paths"]


def test_a_200_with_products_counts_as_answered(server):
    assert calls.call(server.server_address[1]) is True


def test_a_503_counts_as_failed(server):
    server.RequestHandlerClass.status = 503
    assert calls.call(server.server_address[1]) is False


def test_a_200_without_products_counts_as_failed(server):
    server.RequestHandlerClass.body = b"<html>not it</html>"
    assert calls.call(server.server_address[1]) is False


def test_a_refused_connection_counts_as_failed():
    assert calls.call(free_port()) is False


def test_a_reset_connection_counts_as_failed():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        def hang_up():
            connection, _ = listener.accept()
            connection.close()

        thread = threading.Thread(target=hang_up, daemon=True)
        thread.start()
        assert calls.call(port) is False
        thread.join(2)


def test_a_call_that_doesnt_answer_in_time_counts_as_failed():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert calls.call(listener.getsockname()[1], timeout=0.1) is False


def fake_run(answers, seconds=None):
    """calls.run on a fake clock: answers(second) says whether that second's calls answer."""
    out = io.StringIO()
    moment = [1000.0]

    def caller():
        return answers(moment[0] - 1000.0)

    def sleep(duration):
        moment[0] += duration

    original = calls.concurrent.futures.ThreadPoolExecutor

    class Now(original):
        """Runs each call at once, so the counts don't depend on threads."""

        def submit(self, function):
            function()

    calls.concurrent.futures.ThreadPoolExecutor = Now
    try:
        tally = calls.run(seconds, out=out, caller=caller, clock=lambda: moment[0], wall=lambda: moment[0], sleep=sleep)
    finally:
        calls.concurrent.futures.ThreadPoolExecutor = original
    return tally, out.getvalue()


def rows(text):
    return [ANSI.sub("", line) for line in text.splitlines()]


def test_a_line_a_second_with_running_totals_and_the_header_every_ten():
    _, text = fake_run(lambda second: True, seconds=25)
    lines = rows(text)
    assert lines[0] == calls.HEADER
    assert lines.count(calls.HEADER) == 3
    figures = [line.split() for line in lines if line != calls.HEADER]
    assert len(figures) == 25
    assert [row[1] for row in figures[:3]] == ["10", "20", "30"] and {row[2] for row in figures} == {"0"}


def test_failed_totals_climb_and_stop_and_it_says_when_calls_are_back():
    tally, text = fake_run(lambda second: not 5 <= second < 12, seconds=25)
    lines = rows(text)
    said = [line for line in lines if "Answering again" in line]
    assert said == ["Answering again: 70 calls failed."]
    assert tally.failed == 70 and tally.answered == 180
    assert tally.first_failed == pytest.approx(1005.0, abs=0.11) and tally.last_failed == pytest.approx(1011.9, abs=0.11)
    # Not before five clean seconds after the last failed call: the line says it then.
    index = lines.index(said[0])
    figures = [line for line in lines[:index] if line != calls.HEADER]
    settled = [line for line in figures if line.split()[2] == "70"]
    assert len(settled) == calls.QUIET_SECONDS + 1


def test_it_says_nothing_about_being_back_while_nothing_failed():
    _, text = fake_run(lambda second: True, seconds=20)
    assert "Answering again" not in text


def test_it_says_it_once_per_outage():
    _, text = fake_run(lambda second: not (2 <= second < 4 or 14 <= second < 16), seconds=30)
    said = [line for line in rows(text) if "Answering again" in line]
    assert said == ["Answering again: 20 calls failed.", "Answering again: 40 calls failed."]


def test_the_failed_figure_is_red_only_in_the_second_it_rises():
    _, text = fake_run(lambda second: second >= 3, seconds=6)
    lines = text.splitlines()
    assert sum(calls.RED in line for line in lines) == 3
    _, text = fake_run(lambda second: second < 3, seconds=6)
    red = [line for line in text.splitlines() if calls.RED in line]
    assert len(red) == 3


def test_every_row_fits_the_right_hand_window():
    _, text = fake_run(lambda second: second % 3 == 0, seconds=30)
    assert all(len(line) <= 43 for line in rows(text))


def test_it_never_runs_past_the_give_up_time():
    assert calls.GIVE_UP_SECONDS == 1200
    _, text = fake_run(lambda second: True)
    figures = [line for line in rows(text) if line != calls.HEADER]
    assert len(figures) == calls.GIVE_UP_SECONDS


def test_the_summary_is_what_first_day_reads():
    tally, _ = fake_run(lambda second: not 2 <= second < 3, seconds=5)
    line = calls.summary(tally)
    assert line.startswith("== summary ")
    read = json.loads(line.removeprefix("== summary "))
    assert read["failed"] == 10 and read["answered"] == 40 and read["first_failed"] < read["last_failed"]


def test_real_calls_against_a_real_server_at_the_real_rate(server):
    out = io.StringIO()
    port = server.server_address[1]
    tally = calls.run(2, out=out, caller=lambda: calls.call(port))
    assert tally.failed == 0 and tally.answered == 20
    assert len([line for line in rows(out.getvalue()) if line != calls.HEADER]) == 2


def test_it_needs_only_the_standard_library():
    text = (Path(calls.__file__)).read_text()
    imported = set(re.findall(r"^(?:from|import) ([a-z_]+)", text, re.M))
    assert imported <= {"argparse", "concurrent", "datetime", "http", "json", "sys", "threading", "time", "zoneinfo"}
