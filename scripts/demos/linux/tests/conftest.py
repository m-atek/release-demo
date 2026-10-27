import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
# Like the scripts themselves: the shared scripts first, then this demo's.
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import run_state  # noqa: E402


def load(name, path):
    """A script under its own module name, so it can't collide with a shared one."""
    loader = SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


def result(server, outcome, running, previous="2.7.3", target="2.8.0", detail="", site=None):
    """One server's result as ansible/tasks/result.yml writes it."""
    site = site or ("cloud" if server.startswith("inv-cloud") else "data-center")
    return {"server": server, "site": site, "location": "AWS eu-west-1" if site == "cloud" else "Data center",
            "action": "deploy", "result": outcome, "target": target, "running": running, "previous": previous,
            "java": "11.0.32.1", "started_at": 100.0, "finished_at": 124.2, "detail": detail,
            "steps": [{"at": 110.0, "what": "switched to " + target}, {"at": 118.0, "what": "check ok"}]}


JAVA8 = ("FAIL /health answered 404 after 4s, expected 2.8.0. Tomcat: UnsupportedClassVersionError: built for "
         "Java 11 (class file 55.0), this server runs Java 8 (up to 52.0)")
