"""Deploy, check, /ask, reset and search, with fakes for SSM, the index and Claude."""

import json
from types import SimpleNamespace

import pytest

import rag
import run_state
from conftest import STATES, FakeDatabricks


class FakeSSM:
    """Two pointers with their history, as SSM keeps them."""

    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, values=None):
        self.history = {name: [json.dumps(v)] for name, v in (values or {}).items()}

    def get_parameter(self, Name, WithDecryption=False):
        found = self.history.get(Name.removeprefix(rag.POINTER_PREFIX))
        return {"Parameter": {"Value": found[-1] if found else "unset"}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.history.setdefault(Name.removeprefix(rag.POINTER_PREFIX), []).append(Value)

    def get_parameter_history(self, Name, MaxResults, **kwargs):
        values = self.history.get(Name.removeprefix(rag.POINTER_PREFIX), [])
        return {"Parameters": [{"Value": v} for v in values]}


def pointer(version, index="c.runbook_assistant.chunks_aaaa_index"):
    return {"version": version, "sha256": version * 3, "package": f"p/{version}.zip", "index": index,
            "build": "1", "at": "2026-10-30T10:00:00Z"}


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSSM({"runbook-assistant/production": pointer("2.0.0"), "runbook-assistant/staging": pointer("2.0.0")})
    monkeypatch.setattr(rag, "deploy_ssm", lambda: fake)
    monkeypatch.setattr(rag, "ssm_client", lambda: fake)
    return fake


def test_rollback_goes_to_the_last_other_version():
    history = [pointer("2.0.0"), pointer("2.1.1"), pointer("2.1.1")]
    assert rag.previous_pointer(history)["version"] == "2.0.0"
    assert rag.previous_pointer([pointer("2.0.0")]) is None


def test_deploy_previous_moves_production_back_and_says_from_what(ssm):
    import deploy

    rag.write_pointer("production", pointer("2.1.1"), ssm)
    state = {"deploys": {}}
    deploy.previous("production", state)
    assert json.loads(ssm.history["runbook-assistant/production"][-1])["version"] == "2.0.0"
    assert state["deploys"]["production"]["previous_version"] == "2.1.1"
    assert state["deploys"]["production"]["rolled_back_to"] == "2.0.0"


def test_deploy_back_restores_this_runs_previous_pointer(ssm):
    import deploy

    state = {"deploys": {"staging": {"previous_pointer": pointer("2.0.0")}}}
    rag.write_pointer("staging", pointer("2.1.0"), ssm)
    deploy.back("staging", state)
    assert json.loads(ssm.history["runbook-assistant/staging"][-1])["version"] == "2.0.0"


def test_a_deploy_writes_the_pointer_the_answer_path_reads(ssm, monkeypatch):
    import deploy
    import index

    monkeypatch.setattr(rag, "fetch", lambda path, sha256, token=None: STATES / "2.1.0")
    monkeypatch.setattr(deploy, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(deploy, "ssm_client", lambda: ssm)
    monkeypatch.setattr(index, "ensure", lambda folder, sha256=None: "c.runbook_assistant.chunks_bbbb_index")
    state = {"version": "2.1.0", "package": {"path": "p/2.1.0.zip", "sha256": "s" * 64}, "build": {"number": "7"}}
    run_state.save(state)
    deploy.deploy("staging", state)
    written = json.loads(ssm.history["runbook-assistant/staging"][-1])
    assert (written["version"], written["sha256"], written["index"], written["build"]) == \
        ("2.1.0", "s" * 64, "c.runbook_assistant.chunks_bbbb_index", "7")
    assert state["deploys"]["staging"]["previous_version"] == "2.0.0"


def test_with_the_endpoint_down_staging_stops_at_once():
    import index

    db = SimpleNamespace(endpoint=lambda name: None)
    with pytest.raises(SystemExit) as stop:
        index.ensure(STATES / "2.1.0", db=db, catalog_name="c")
    assert stop.value.code == rag.ENDPOINT_DOWN


def test_the_smoke_check_asks_three_questions_in_staging(ssm, monkeypatch):
    import answer
    import check

    asked = []

    def fake(question, environment, db=None, pointer=None, spans=None):
        asked.append(question)
        return {"answer": "From the runbooks.", "sources": [{"id": "RB-06", "title": "x", "status": "current"}],
                "refused": False, "tokens": {"output": 30}}

    monkeypatch.setattr(answer, "answer", fake)
    results = check.check("staging", "2.0.0", db=FakeDatabricks([]))
    assert all(ok for ok, _ in results) and len(asked) == 3
    assert not all(ok for ok, _ in check.check("staging", "2.1.0", db=FakeDatabricks([])))


def test_ask_answers_with_its_sources_and_version():
    import ask

    text = ask.answered("How do I roll back orders-service?", "U0SAM000001",
                        {"answer": "Type /rollback orders-service in #releases.", "version": "2.1.1",
                         "sources": [{"id": "RB-07", "title": "Roll back a release", "status": "current"}]})
    assert text.split("\n\n") == ["Question from <@U0SAM000001>: How do I roll back orders-service?",
                                  "Type /rollback orders-service in #releases.",
                                  "Sources: RB-07 Roll back a release (current)",
                                  "Answered by runbook-assistant 2.1.1"]


def test_ask_refuses_a_question_holding_a_secret(monkeypatch):
    import ask
    import llm

    posted = []
    monkeypatch.setattr(llm, "redact", lambda text: ("[removed]", 1))
    monkeypatch.setattr(ask, "update", posted.append)
    monkeypatch.setenv("QUESTION", "Why does my key fail?")
    assert ask.main() == 0
    assert posted[0].endswith(ask.SECRET) and "my key" not in posted[0]


def test_ask_passes_the_endpoints_message_on(monkeypatch):
    import answer
    import ask
    import llm

    posted = []
    monkeypatch.setattr(llm, "redact", lambda text: (text, 0))
    monkeypatch.setattr(ask, "update", posted.append)

    def down(question, environment):
        raise SystemExit(rag.ENDPOINT_DOWN)

    monkeypatch.setattr(answer, "answer", down)
    monkeypatch.setenv("QUESTION", "How do I roll back orders-service?")
    assert ask.main() == 1 and posted[0].endswith(rag.ENDPOINT_DOWN)


def test_reset_finds_2_0_0_in_productions_history():
    import importlib.util

    spec = importlib.util.spec_from_file_location("rag_reset", rag.HERE / "reset.py")
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    history = [pointer("2.0.0", "i1"), pointer("2.1.1", "i1"), pointer("2.0.0", "i2"), pointer("2.1.1", "i2")]
    assert reset.baseline_pointer(history)["index"] == "i2"
    assert reset.baseline_pointer([pointer("2.1.1")]) is None


def test_up_rebuilds_the_indexes_the_pointers_name_and_2_0_0s(ssm):
    import search

    names = search.wanted_indexes(ssm, "c")
    assert names[0] == "c.runbook_assistant.chunks_aaaa_index"
    assert names[-1] == rag.index_name(rag.chunk_hash(STATES / "2.0.0"), catalog_name="c")


def test_the_evaluation_runs_after_check_staging_for_this_service_only():
    from common import service

    stages = run_state.stages_for(service("runbook-assistant"))
    assert stages.index("Evaluation") == stages.index("Check staging") + 1
    assert stages.index("Change") == stages.index("Evaluation") + 1
    assert len(run_state.stages_for(service("orders-service"))) == 12
    assert "Evaluation" not in run_state.stages_for(service("pricing-api"))
