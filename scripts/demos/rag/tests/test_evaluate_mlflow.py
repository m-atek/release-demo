"""evaluate.py's MLflow side against the real MLflow 3 library, with a local
tracking store and fakes for Databricks and Claude. Skipped where MLflow isn't
installed (the evaluation's own environment has it: scripts/demos/rag/requirements.txt)."""

import json
from types import SimpleNamespace

import pytest

mlflow = pytest.importorskip("mlflow.genai")

import evaluate  # noqa: E402
import rag  # noqa: E402
from conftest import STATES  # noqa: E402

POINTER = {"version": "2.1.0", "sha256": "s" * 64, "package": "p.zip", "index": "c.runbook_assistant.chunks_a_index"}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    import mlflow as ml

    import answer
    import databricks_api as databricks
    import llm

    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")  # mlflow-skinny has no database store; the test needs none
    ml.set_tracking_uri((tmp_path / "mlruns").as_uri())
    monkeypatch.setattr(evaluate, "connect", lambda ssm: ml)
    monkeypatch.setattr(evaluate, "get_parameter", lambda ssm, name: "https://workspace.invalid")
    monkeypatch.setattr(rag, "read_pointer", lambda environment, ssm=None: POINTER)
    monkeypatch.setattr(rag, "fetch", lambda path, sha256, token=None: STATES / "2.1.0")
    monkeypatch.setattr(databricks.Databricks, "as_pipeline", classmethod(lambda cls, ssm=None: object()))

    def fake_answer(question, environment, spans=None, db=None, pointer=None):
        retired = "roll back" in question
        page = ("RB-02", "Release checklist: orders-service", "retired") if retired else ("RB-06", "Quality gate", "current")
        chunks = [{"page_id": page[0], "chunk_id": f"{page[0]}#1", "title": page[1], "status": page[2],
                   "text": f"{page[0]} {page[1]}", "score": 0.9}]
        with spans.retriever({"query": question}) as done:
            done(chunks)
        with spans.llm({"question": question}) as done:
            done({"answer": "An answer."})
        return {"answer": "An answer.", "sources": [{"id": page[0], "title": page[1], "status": page[2]}],
                "chunks": [{k: c[k] for k in ("page_id", "chunk_id", "status", "score")} for c in chunks],
                "passages": [{"page": page[0], "title": page[1], "text": chunks[0]["text"]}],
                "refused": False, "version": "2.1.0", "model": "claude-haiku-5-5", "cost": 0.0001}

    def fake_judge(task, system, user, schema, service, model=None, effort=None, record=True, sdk=None):
        assert task == "judge" and model == "claude-sonnet-5-5" and "passages" in user
        return SimpleNamespace(data={"correct": True, "correct_reason": "States the facts.", "grounded": True,
                                     "grounded_reason": "From the passages."}, cost=0.001, model=model)

    monkeypatch.setattr(answer, "answer", fake_answer)
    monkeypatch.setattr(llm, "complete", fake_judge)
    return ml


def test_an_evaluation_run_is_named_tagged_and_counted(wired):
    state = {"version": "2.1.0", "package": {"sha256": "s" * 64}, "build": {"number": "41", "url": "https://j/41"},
             "commit": "abc", "jira_key": "ORD-111"}
    record, found, questions_sha256, judge_model, url, experiment_id = evaluate.evaluate(state, None)
    assert found["total"] == 40 and found["correct"] == 40
    # Q01 and the other rollback question answer from RB-02 here: two retired pages.
    assert found["retired"] == sum(1 for q in rag.questions()[0] if "roll back" in q["question"])
    run = wired.search_runs(experiment_ids=[experiment_id], output_format="list")[0]
    assert run.info.run_name == "runbook-assistant 2.1.0 (build #41)"
    assert run.data.tags["questions_sha256"] == questions_sha256 and run.data.tags["judge_model"] == "claude-sonnet-5-5"
    assert run.data.tags["package_sha256"] == "s" * 64 and run.data.tags["jira_key"] == "ORD-111"
    assert run.data.metrics["retired_count"] == found["retired"]
    assert record.rows["Q01"]["retired_reason"] == "Cites RB-02, retired, replaced by RB-07."
    traces = wired.search_traces(experiment_ids=[experiment_id], return_type="list", max_results=50)
    spans = [span for trace in traces for span in trace.data.spans]
    retrievers = [span for span in spans if span.span_type == "RETRIEVER"]
    assert retrievers and retrievers[0].outputs[0]["metadata"]["status"] in ("retired", "current")
    assert json.dumps(evaluate.production_scores(wired, experiment_id, "2.1.0")) == json.dumps(
        {"version": "2.1.0", "correct": 40, "grounded": 40, "retired": found["retired"]})
