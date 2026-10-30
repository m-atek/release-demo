"""Check staging and Check production for the runbook assistant: is it there, and does it answer?

    python scripts/demos/rag/check.py runbook-assistant staging --version 2.1.0 --url ""

The pointer must name the version, and its index must be online. Then the
smoke questions, the first 3 in staging and all 5 in production: each answer
must come back in the schema, with at least one page. One line each, ok or
FAIL. It checks availability, not quality: that is the evaluation's job.
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402


def smoke(question, environment, pointer, db):
    import answer

    try:
        found = answer.answer(question["question"], environment, db=db, pointer=pointer)
    except SystemExit as error:
        return False, str(error.code) if isinstance(error.code, str) else "the answer path stopped"
    except Exception as error:
        return False, f"{error.__class__.__name__}: {error}"
    if found["refused"]:
        return False, found["answer"]
    if not isinstance(found["answer"], str) or not found["answer"].strip() or not found["sources"]:
        return False, "no answer with a page"
    return True, f"{', '.join(s['id'] for s in found['sources'])} ({found['tokens']['output']} tokens out)"


def check(environment, version, db=None, pointer=None):
    from databricks_api import Databricks

    config = rag.config()["environments"][environment]["check"]
    pointer = pointer or rag.read_pointer(environment)
    results = []
    if not pointer or pointer["version"] != version:
        results.append((False, f"{environment} points at {(pointer or {}).get('version') or 'nothing'}, not {version}"))
        return results
    results.append((True, f"{environment} points at {version}"))
    db = db or Databricks.as_pipeline()
    index = db.index(pointer["index"]) or {}
    if not (index.get("status") or {}).get("ready"):
        results.append((False, f"the index {pointer['index']} is online ({rag.ENDPOINT_DOWN})"))
        return results
    results.append((True, f"the index {pointer['index'].rsplit('.', 1)[1]} is online"))
    questions = rag.smoke_questions(config["questions"])
    with ThreadPoolExecutor(max_workers=len(questions) or 1) as pool:
        answers = list(pool.map(lambda q: smoke(q, environment, pointer, db), questions))
    for question, (ok, detail) in zip(questions, answers):
        results.append((ok, f"{question['id']} {question['question']}: {detail}"))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment", choices=["staging", "production"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", default="")
    args = parser.parse_args()
    started = time.monotonic()
    results = check(args.environment, args.version)
    for ok, what in results:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
    print(f"{time.monotonic() - started:.0f}s")
    sys.exit(0 if all(ok for ok, _ in results) else 1)


if __name__ == "__main__":
    main()
