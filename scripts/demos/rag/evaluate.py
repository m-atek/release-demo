"""Evaluation, after Check staging and before Change: the same forty questions for every version.

    python scripts/demos/rag/evaluate.py runbook-assistant

Runs mlflow.genai.evaluate on the questions in questions.yaml, with 8
workers, against staging's pointer: the package Staging just deployed, the
same SHA-256 as this run's. For each question, four scorers:
- correct and grounded, from one judge call (claude-sonnet-5-5 at low
  effort, the prompt judge.md, a one-sentence reason each);
- retired_page, code: fails if any cited page is retired;
- expected_source, code: passes if an expected page is cited.

The run goes to the experiment /Shared/runbook-assistant as
"runbook-assistant <version> (build #<n>)", tagged with the version, the
package's SHA-256, the commit, the Jira key, the build, the questions'
SHA-256 and the judge model. It writes .release/reports/evaluation.json (per
question) and evaluation.txt, and prints the run's Claude cost (never to
Slack).

The gate is code: it reads the counts and compares them with services.yaml.
A model writes answers and judgments only; nothing it writes approves,
rejects or changes a release. If Claude, the judge or Databricks fails on
any question, the stage fails with the reason: a release never passes a
gate it couldn't run.
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402
import run_state  # noqa: E402
from common import fail, get_parameter, ROOT, ssm_client  # noqa: E402

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"correct": {"type": "boolean"}, "correct_reason": {"type": "string"},
                   "grounded": {"type": "boolean"}, "grounded_reason": {"type": "string"}},
    "required": ["correct", "correct_reason", "grounded", "grounded_reason"],
    "additionalProperties": False,
}


# The record of one run, filled in as the questions are answered and scored.

class Record:
    def __init__(self, questions):
        self.lock = threading.Lock()
        self.rows = {q["id"]: {"id": q["id"], "question": q["question"]} for q in questions}
        self.errors = {}
        self.cost = 0.0

    def put(self, qid, **values):
        with self.lock:
            self.rows[qid].update(values)

    def error(self, qid, where, error):
        with self.lock:
            self.errors.setdefault(qid, f"{where}: {error}")

    def spend(self, cost):
        with self.lock:
            self.cost += cost


def resolve_sources(question, changes):
    """Page ids for the expected sources: a past change named by its short description
    becomes the change numbers changes.json holds for it."""
    found = []
    for source in question.get("expected_sources") or []:
        if isinstance(source, dict):
            found += [c["number"] for c in changes if c["short_description"] == source["change"]]
        else:
            found.append(source)
    return found


def counts(rows):
    total = len(rows)
    return {"total": total,
            "correct": sum(1 for r in rows if r.get("correct") is True),
            "grounded": sum(1 for r in rows if r.get("grounded") is True),
            "retired": sum(1 for r in rows if r.get("retired_page") is False),
            "expected_source": sum(1 for r in rows if r.get("expected_source") is True)}


def judge_gate(found, gate):
    """(passed, the Slack line). The gate in services.yaml decides, nothing else."""
    total = found["total"]
    failed = []
    if found["retired"] > gate["retired_page_max"]:
        failed.append(f"{found['retired']} of {total} answers used a retired page (allowed {gate['retired_page_max']})")
    if found["correct"] < gate["correct_min"]:
        failed.append(f"correct {found['correct']} of {total} (needs {gate['correct_min']})")
    if found["grounded"] < gate["grounded_min"]:
        failed.append(f"grounded {found['grounded']} of {total} (needs {gate['grounded_min']})")
    if failed:
        return False, "Evaluation failed: " + "; ".join(failed) + "."
    return True, (f"Evaluation passed: correct {found['correct']} of {total}, grounded {found['grounded']} of {total}, "
                  "none from a retired page.")


def change_details(found, gate, questions_sha256, judge_model, production, run_url):
    """The lines change.py adds to the change's description: what the evaluation proved, against what."""
    total = found["total"]
    lines = [
        f"Evaluation: {total} fixed questions (questions.yaml, SHA-256 {questions_sha256}), judged by {judge_model}.",
        f"Correct {found['correct']} of {total} (needs {gate['correct_min']}). Grounded {found['grounded']} of {total} "
        f"(needs {gate['grounded_min']}). Answers from a retired page: {found['retired']} "
        f"(allowed {gate['retired_page_max']}).",
    ]
    if production:
        lines.append(f"Production runs {production['version']}: correct {production['correct']}, grounded "
                     f"{production['grounded']}, from a retired page {production['retired']}, at its own release.")
    lines.append(f"MLflow run: {run_url}")
    return lines


def retired_reason(sources, replaced_by):
    retired = [s for s in sources if s.get("status") == "retired"]
    if not retired:
        return True, "No retired page cited."
    parts = [f"{s['id']}, retired" + (f", replaced by {replaced_by[s['id']]}" if replaced_by.get(s["id"]) else "")
             for s in retired]
    return False, "Cites " + "; ".join(parts) + "."


def write_reports(record, summary, folder):
    folder.mkdir(parents=True, exist_ok=True)
    rows = [record.rows[qid] for qid in sorted(record.rows)]
    (folder / "evaluation.json").write_text(json.dumps({**summary, "questions": rows}, indent=2) + "\n")
    lines = [f"runbook-assistant {summary['version']} evaluation: {summary['line']}",
             f"Package SHA-256 {summary['package_sha256']}, questions SHA-256 {summary['questions_sha256']}, "
             f"judge {summary['judge_model']}.", f"MLflow run: {summary['mlflow_run_url']}", ""]
    for row in rows:
        marks = " ".join(f"{name}={'pass' if row.get(name) else 'FAIL'}"
                         for name in ("correct", "grounded", "retired_page", "expected_source"))
        lines.append(f"{row['id']} {marks}  {row['question']}")
        lines.append(f"    answer: {row.get('answer', '')}")
        lines.append(f"    sources: {', '.join(s['id'] + ' (' + s['status'] + ')' for s in row.get('sources', []))}")
    (folder / "evaluation.txt").write_text("\n".join(lines) + "\n")


# MLflow

class Spans:
    """The answer path's two steps as MLflow spans: the search, with each page's status, and the call."""

    def __init__(self, mlflow):
        self.mlflow = mlflow

    def retriever(self, inputs):
        import contextlib

        from mlflow.entities import Document, SpanType

        @contextlib.contextmanager
        def span():
            with self.mlflow.start_span(name="search", span_type=SpanType.RETRIEVER) as current:
                current.set_inputs(inputs)
                yield lambda chunks: current.set_outputs([
                    Document(page_content=c["text"], id=c["chunk_id"],
                             metadata={"page_id": c["page_id"], "title": c["title"], "status": c["status"],
                                       "score": c["score"]}) for c in chunks])
        return span()

    def llm(self, inputs):
        import contextlib

        from mlflow.entities import SpanType

        @contextlib.contextmanager
        def span():
            with self.mlflow.start_span(name="answer", span_type=SpanType.LLM) as current:
                current.set_inputs(inputs)  # the model, the effort and the question; the passages are the search's
                yield current.set_outputs
        return span()


def connect(ssm):
    """MLflow on the workspace's tracking server, as the service principal. Nothing is printed."""
    import mlflow

    os.environ["DATABRICKS_HOST"] = get_parameter(ssm, "databricks/host")
    os.environ["DATABRICKS_CLIENT_ID"] = get_parameter(ssm, "databricks/client-id")
    os.environ["DATABRICKS_CLIENT_SECRET"] = get_parameter(ssm, "databricks/client-secret")
    mlflow.set_tracking_uri("databricks")
    return mlflow


def production_scores(mlflow, experiment_id, version):
    """Production's version, with its counts at its own release, from that run's metrics."""
    if not version:
        return None
    runs = mlflow.search_runs(experiment_ids=[experiment_id], filter_string=f"tags.version = '{version}'",
                              order_by=["attributes.start_time DESC"], max_results=1, output_format="list")
    if not runs:
        return None
    metrics = runs[0].data.metrics
    return {"version": version, "correct": int(metrics.get("correct_count", -1)),
            "grounded": int(metrics.get("grounded_count", -1)), "retired": int(metrics.get("retired_count", -1))}


def evaluate(state, ssm):
    import llm
    from mlflow.entities import Feedback
    from mlflow.genai.scorers import scorer

    import answer as answer_path
    from databricks_api import Databricks

    config = rag.config()
    settings = config["evaluation"]
    pointer = rag.read_pointer("staging", ssm)
    if not pointer or pointer["sha256"] != state["package"]["sha256"]:
        fail(f"Staging runs {(pointer or {}).get('sha256', 'nothing')[:12]}, not this run's package "
             f"{state['package']['sha256'][:12]}: the evaluation tests what ships, or nothing.")
    questions, questions_sha256 = rag.questions()
    folder = rag.fetch(pointer["package"], pointer["sha256"])
    changes = json.loads((folder / "corpus" / "changes.json").read_text())
    replaced_by = {pid: page["replaced_by"] for pid, page in rag.pages_of(folder).items() if page["replaced_by"]}
    judge = settings["judge"]
    judge_prompt = (ROOT / judge["prompt"]).read_text()
    record = Record(questions)
    db = Databricks.as_pipeline(ssm)
    mlflow = connect(ssm)
    spans = Spans(mlflow)
    experiment = mlflow.set_experiment(config["databricks"]["experiment"])

    def predict(question, question_id):
        try:
            found = answer_path.answer(question, "staging", spans=spans, db=db, pointer=pointer)
        except (Exception, SystemExit) as error:
            record.error(question_id, "answer", error)
            raise
        record.spend(found["cost"])
        record.put(question_id, answer=found["answer"], sources=found["sources"], chunks=found["chunks"],
                   refused=found["refused"])
        return {"answer": found["answer"], "sources": found["sources"], "passages": found["passages"],
                "version": found["version"], "model": found["model"]}

    @scorer
    def correctness(inputs, outputs, expectations):
        """One judge call: correct and grounded, each with its reason."""
        qid = expectations["id"]
        try:
            result = llm.complete("judge", system=judge_prompt, schema=JUDGE_SCHEMA, service=rag.SERVICE,
                                  model=judge["model"], effort=judge["effort"],
                                  user={"question": inputs["question"], "expected_facts": expectations["expected_facts"],
                                        "passages": outputs["passages"], "answer": outputs["answer"]})
        except (Exception, SystemExit) as error:
            record.error(qid, "judge", error)
            raise
        record.spend(result.cost)
        data = result.data
        record.put(qid, correct=bool(data["correct"]), correct_reason=data["correct_reason"],
                   grounded=bool(data["grounded"]), grounded_reason=data["grounded_reason"])
        return [Feedback(name="correct", value=bool(data["correct"]), rationale=data["correct_reason"]),
                Feedback(name="grounded", value=bool(data["grounded"]), rationale=data["grounded_reason"])]

    @scorer
    def retired_page(outputs, expectations):
        ok, why = retired_reason(outputs["sources"], replaced_by)
        record.put(expectations["id"], retired_page=ok, retired_reason=why)
        return Feedback(value=ok, rationale=why)

    @scorer
    def expected_source(outputs, expectations):
        wanted = expectations["expected_sources"]
        cited = [s["id"] for s in outputs["sources"]]
        ok = any(s in cited for s in wanted)
        why = (f"Cites {', '.join(s for s in cited if s in wanted)}." if ok
               else f"Cites {', '.join(cited) or 'nothing'}; expected {', '.join(wanted) or 'a page'}.")
        record.put(expectations["id"], expected_source=ok, expected_reason=why)
        return Feedback(value=ok, rationale=why)

    data = [{"inputs": {"question": q["question"], "question_id": q["id"]},
             "expectations": {"id": q["id"], "expected_facts": q["expected_facts"],
                              "expected_sources": resolve_sources(q, changes)}} for q in questions]
    os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(settings["workers"])
    run_name = f"runbook-assistant {state['version']} (build #{state['build']['number']})"
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.set_tags({"version": state["version"], "package_sha256": state["package"]["sha256"],
                         "commit": state.get("commit", ""), "jira_key": state.get("jira_key", ""),
                         "build_url": state["build"].get("url", ""), "questions_sha256": questions_sha256,
                         "judge_model": judge["model"], "index": pointer["index"]})
        mlflow.genai.evaluate(data=data, predict_fn=predict, scorers=[correctness, retired_page, expected_source])
        if record.errors:
            first = sorted(record.errors.items())[0]
            fail(f"Evaluation couldn't run on {len(record.errors)} of {len(questions)} questions; "
                 f"{first[0]}: {first[1]}. A release never passes a gate it couldn't run.")
        found = counts(list(record.rows.values()))
        mlflow.log_metrics({"correct_count": found["correct"], "grounded_count": found["grounded"],
                            "retired_count": found["retired"], "expected_source_count": found["expected_source"],
                            "claude_cost_usd": round(record.cost, 4)})
        run_id = run.info.run_id
    url = f"{get_parameter(ssm, 'databricks/host').rstrip('/')}/ml/experiments/{experiment.experiment_id}/runs/{run_id}"
    return record, found, questions_sha256, judge["model"], url, experiment.experiment_id


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    args = parser.parse_args()
    if args.service != rag.SERVICE:
        fail(f"This evaluates {rag.SERVICE}, not {args.service}.")
    import rag_env

    rag_env.ensure()
    import notify

    ssm = ssm_client()
    state = run_state.load()
    started = time.monotonic()
    record, found, questions_sha256, judge_model, url, experiment_id = evaluate(state, ssm)
    gate = rag.config()["evaluation"]["gate"]
    passed, line = judge_gate(found, gate)
    import mlflow

    production = production_scores(mlflow, experiment_id, (rag.read_pointer("production", ssm) or {}).get("version"))
    summary = {"version": state["version"], "package_sha256": state["package"]["sha256"],
               "questions_sha256": questions_sha256, "judge_model": judge_model, "counts": found, "gate": gate,
               "passed": passed, "line": line, "mlflow_run_url": url, "claude_cost_usd": round(record.cost, 4),
               "seconds": round(time.monotonic() - started)}
    write_reports(record, summary, run_state.REPORTS)
    print(line)
    print(f"Claude cost for this run: USD {record.cost:.2f} (answers and judge, from llm.py's prices)")
    state = run_state.load()
    state.setdefault("lines", []).append(line)
    if passed:
        state["change_details"] = (state.get("change_details") or []) + change_details(
            found, gate, questions_sha256, judge_model, production, url)
        state["change_attachments"] = [str(run_state.REPORTS / "evaluation.json")]
    run_state.save(state)
    notify.post(state)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
