"""The answer path, with a fake index and a fake Claude."""

import json
from types import SimpleNamespace

import pytest

import answer
import rag
from conftest import STATES, FakeDatabricks

POINTER = {"version": "2.1.0", "sha256": "f" * 64, "package": "demos-candidates/x.zip", "index": "c.s.chunks_1_index"}
ROWS = [{"chunk_id": "RB-02#2", "page_id": "RB-02", "title": "Release checklist: orders-service", "status": "retired",
         "text": "RB-02 Release checklist: orders-service\nRolling back\nAsk Pieter to put the previous package back.",
         "score": 0.91},
        {"chunk_id": "RB-07#2", "page_id": "RB-07", "title": "Roll back a release", "status": "current",
         "text": "RB-07 Roll back a release\nBy hand, with /rollback\nType /rollback orders-service.", "score": 0.88}]


@pytest.fixture
def package(monkeypatch):
    monkeypatch.setattr(rag, "fetch", lambda path, sha256, token=None: STATES / "2.1.0")


@pytest.fixture
def claude(monkeypatch):
    import llm

    fake = SimpleNamespace(asked=[], reply={"answer": "Ask Pieter to put the previous package back.",
                                            "sources": ["RB-02"]})

    def complete(task, system, user, schema, service, model=None, effort=None, record=True, sdk=None):
        fake.asked.append({"system": system, "user": user, "model": model, "effort": effort})
        return SimpleNamespace(data=fake.reply, model=model, tokens={"input": 900, "output": 40}, cost=0.0001,
                               removed=0)

    monkeypatch.setattr(llm, "complete", complete)
    return fake


def test_the_package_s_search_arguments_become_the_rest_query():
    body = answer.rest_query({"query_text": "q", "columns": ["a"], "filters": {"status": "current"}, "num_results": 5})
    assert body == {"query_text": "q", "columns": ["a"], "num_results": 5, "filters_json": '{"status": "current"}'}
    hybrid = answer.rest_query({"query_text": "q", "columns": ["a"], "query_type": "hybrid", "num_results": 5})
    assert hybrid["query_type"] == "HYBRID" and "filters_json" not in hybrid


def test_2_1_0_asks_hybrid_without_the_filter_and_answers_from_a_retired_page(package, claude):
    db = FakeDatabricks(ROWS)
    found = answer.answer("How do I roll back orders-service?", "staging", db=db, pointer=POINTER)
    assert db.queries[0]["query_type"] == "HYBRID" and "filters_json" not in db.queries[0]
    assert found["sources"] == [{"id": "RB-02", "title": "Release checklist: orders-service", "status": "retired"}]
    assert found["version"] == "2.1.0" and not found["refused"]
    assert claude.asked[0]["model"] == "claude-haiku-5-5" and claude.asked[0]["effort"] == "low"


def test_the_model_sees_ids_and_titles_but_no_status(package, claude):
    answer.answer("How do I roll back orders-service?", "staging", db=FakeDatabricks(ROWS), pointer=POINTER)
    user = claude.asked[0]["user"]
    assert "[RB-02] Release checklist: orders-service" in user and "[RB-07] Roll back a release" in user
    assert "retired" not in user and "current" not in user


def test_an_answer_citing_a_page_it_wasnt_given_is_refused(package, claude):
    claude.reply = {"answer": "Use RB-99.", "sources": ["RB-99"]}
    found = answer.answer("q?", "staging", db=FakeDatabricks(ROWS), pointer=POINTER)
    assert found["refused"] and found["answer"] == answer.CITED_UNKNOWN and found["sources"] == []


def test_with_the_endpoint_down_the_message_says_what_to_do(package, claude):
    with pytest.raises(SystemExit) as stop:
        answer.answer("q?", "staging", db=FakeDatabricks(ROWS, ready=False), pointer=POINTER)
    assert stop.value.code == rag.ENDPOINT_DOWN == "The search endpoint is down: scripts/demo.sh up rag."


def test_2_0_0_asks_for_current_pages_only(monkeypatch, claude):
    monkeypatch.setattr(rag, "fetch", lambda path, sha256, token=None: STATES / "2.0.0")
    db = FakeDatabricks(ROWS[1:])
    claude.reply = {"answer": "Type /rollback orders-service.", "sources": ["RB-07"]}
    answer.answer("q?", "production", db=db, pointer={**POINTER, "version": "2.0.0"})
    assert json.loads(db.queries[0]["filters_json"]) == {"status": "current"} and "query_type" not in db.queries[0]
