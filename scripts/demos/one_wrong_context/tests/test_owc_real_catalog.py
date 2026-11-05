"""The real caller against real catalog processes: a real SIGTERM, a real new process, real sockets."""

import io
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import calls

STATES = Path(__file__).resolve().parents[4] / "demo-states" / "catalog"
ANSI = re.compile(r"\033\[[0-9;]*m")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start(version, port):
    state = STATES / version
    process = subprocess.Popen([sys.executable, "app.py"], cwd=state, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env={**os.environ, "PORT": str(port), "CATALOG_DB_URL": f"file://{state}/data/catalog.json",
                                    "ENVIRONMENT": "production", "POD_NAME": "here"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not calls.call(port):
        time.sleep(0.05)
    assert calls.call(port), f"catalog {version} didn't answer"
    return process


@pytest.mark.parametrize("version", ["1.8.2", "1.9.1"])  # the baseline, and the take's release
def test_both_versions_answer_the_route_the_caller_asks(version):
    port = free_port()
    process = start(version, port)
    try:
        assert calls.call(port) is True
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)


def test_a_process_that_stops_and_comes_back_reads_as_the_window_says(monkeypatch):
    monkeypatch.setattr(calls, "QUIET_SECONDS", 1)
    port = free_port()
    process = start("1.8.2", port)
    out = io.StringIO()
    result = {}
    thread = threading.Thread(target=lambda: result.update(tally=calls.run(5, out=out, caller=lambda: calls.call(port))))
    thread.start()
    try:
        time.sleep(1.2)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        time.sleep(1.0)
        process = start("1.8.2", port)
        thread.join(timeout=20)
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
    tally = result["tally"]
    assert 5 <= tally.failed <= 25 and tally.answered + tally.failed == 50
    assert f"Answering again: {tally.failed} calls failed." in ANSI.sub("", out.getvalue())
