"""The gate and the records, without MLflow or a model: the gate is code."""

import evaluate

GATE = {"retired_page_max": 0, "correct_min": 36, "grounded_min": 36}


def rows(correct=38, grounded=39, retired=0):
    return [{"correct": i < correct, "grounded": i < grounded, "retired_page": i >= retired,
             "expected_source": True} for i in range(40)]


def test_a_retired_page_fails_the_gate_with_the_scripts_line():
    found = evaluate.counts(rows(retired=6))
    assert evaluate.judge_gate(found, GATE) == (
        False, "Evaluation failed: 6 of 40 answers used a retired page (allowed 0).")


def test_every_failed_criterion_joins_before_the_full_stop():
    found = evaluate.counts(rows(correct=31, grounded=35, retired=6))
    assert evaluate.judge_gate(found, GATE)[1] == ("Evaluation failed: 6 of 40 answers used a retired page (allowed 0); "
                                                   "correct 31 of 40 (needs 36); grounded 35 of 40 (needs 36).")


def test_a_pass_says_the_scores():
    assert evaluate.judge_gate(evaluate.counts(rows()), GATE) == (
        True, "Evaluation passed: correct 38 of 40, grounded 39 of 40, none from a retired page.")


def test_the_gate_reads_only_the_counts():
    assert evaluate.judge_gate(evaluate.counts(rows(correct=36, grounded=36)), GATE)[0]
    assert not evaluate.judge_gate(evaluate.counts(rows(correct=35)), GATE)[0]


def test_a_question_that_errored_counts_as_failed():
    found = evaluate.counts([{"correct": None, "grounded": None, "retired_page": None}] + rows()[:39])
    assert found["correct"] == 38 and found["total"] == 40


def test_the_retired_page_reason_names_the_page_and_its_twin():
    sources = [{"id": "RB-02", "title": "Release checklist: orders-service", "status": "retired"}]
    assert evaluate.retired_reason(sources, {"RB-02": "RB-07"}) == (False, "Cites RB-02, retired, replaced by RB-07.")
    assert evaluate.retired_reason([{"id": "RB-07", "status": "current"}], {}) == (True, "No retired page cited.")


def test_the_change_holds_the_scores_the_gate_and_productions():
    lines = evaluate.change_details(evaluate.counts(rows()), GATE, "a" * 64, "claude-sonnet-5-5",
                                    {"version": "2.0.0", "correct": 37, "grounded": 38, "retired": 0},
                                    "https://workspace.invalid/ml/experiments/1/runs/2")
    assert lines[0] == f"Evaluation: 40 fixed questions (questions.yaml, SHA-256 {'a' * 64}), judged by claude-sonnet-5-5."
    assert lines[1] == ("Correct 38 of 40 (needs 36). Grounded 39 of 40 (needs 36). Answers from a retired page: 0 "
                        "(allowed 0).")
    assert lines[2].startswith("Production runs 2.0.0: correct 37, grounded 38")
    assert lines[3].startswith("MLflow run: ")


def test_a_past_change_resolves_to_its_number():
    question = {"expected_sources": [{"change": "Release orders-service 1.2.4 (ORD-5)"}, "RB-07"]}
    changes = [{"number": "CHG0030003", "short_description": "Release orders-service 1.2.4 (ORD-5)"},
               {"number": "CHG0030004", "short_description": "Release orders-service 1.2.5 (ORD-6)"}]
    assert evaluate.resolve_sources(question, changes) == ["CHG0030003", "RB-07"]


def test_the_reports_hold_every_question(tmp_path):
    record = evaluate.Record([{"id": "Q01", "question": "How do I roll back orders-service?"}])
    record.put("Q01", answer="Run /rollback.", sources=[{"id": "RB-07", "status": "current"}], correct=True,
               grounded=True, retired_page=True, expected_source=True)
    summary = {"version": "2.1.1", "package_sha256": "p", "questions_sha256": "q", "judge_model": "m", "line": "ok",
               "mlflow_run_url": "u"}
    evaluate.write_reports(record, summary, tmp_path)
    data = (tmp_path / "evaluation.json").read_text()
    assert '"Q01"' in data and "RB-07 (current)" in (tmp_path / "evaluation.txt").read_text()
