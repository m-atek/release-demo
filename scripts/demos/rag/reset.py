"""Put demo 23 back where a take starts: both environments on 2.0.0, and 2.1.0 staged.

    python scripts/reset.py rag                       or the Jenkins job reset with DEMO=rag
    python scripts/reset.py rag --stage 2.1.1         the second take's version staged instead
    python scripts/reset.py rag --dry-run             every action it would take, and nothing else

Steps, each looking before it acts, so a second run finds nothing to do:
- archive: the main reset's archiving for runbook-assistant: the tags after
  2.0.0 and their GitHub releases under takes/, the release copies in JFrog
  under demos-releases/runbook-assistant/takes/, the Jira versions after
  2.0.0 un-released and ORD-[n] reopened, open changes cancelled;
- pointers: staging and production back on 2.0.0's package and index, from
  production's pointer history;
- stage: the asked version on main, 2.1.0 by default;
- search: the endpoint and 2.0.0's index online, or "Run scripts/demo.sh up rag first.";
- warm: production answers one smoke question, so the first /ask isn't cold.
MLflow runs stay, as all history stays. It runs on the tools host, where JFrog
answers; from the Mac it hands itself over, as the main reset does.
"""

import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # scripts/ first: its host.py and check.py
sys.path.append(str(HERE))
import host  # noqa: E402
import rag  # noqa: E402
from common import ROOT, fail  # noqa: E402

BASELINE = "2.0.0"
STEPS = ["archive", "pointers", "stage", "search", "warm"]
TAKE_ISSUE = "Search the runbooks by exact service names"
UP_FIRST = "Run scripts/demo.sh up rag first."


def main_reset(issues):
    """scripts/reset.py, its steps pointed at runbook-assistant and 2.0.0."""
    spec = importlib.util.spec_from_file_location("shared_reset", ROOT / "scripts" / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    shared.SERVICE, shared.BASELINE, shared.DEMO_ISSUES = rag.SERVICE, BASELINE, issues
    return shared


def take_issue():
    from jira import PROJECT, Jira

    found = Jira().call("POST", "/rest/api/3/search/jql", {"jql": f"project = {PROJECT} AND summary ~ \"{TAKE_ISSUE}\"",
                                                           "fields": ["summary"], "maxResults": 1})
    issues = (found or {}).get("issues") or []
    if not issues:
        fail(f"No Jira issue \"{TAKE_ISSUE}\": run scripts/demos/rag/setup.py --only jira first.")
    return issues[0]["key"]


def baseline_pointer(history):
    """2.0.0's last pointer in production's history: its package and its index."""
    for pointer in reversed(history):
        if pointer["version"] == BASELINE:
            return pointer
    return None


def step_archive(reset, shared):
    shared.cancel_changes(reset)
    shared.github_takes(reset)
    shared.jfrog_takes(reset)
    shared.jira_versions(reset)


def step_pointers(reset, shared):
    history = rag.pointer_history("production")
    baseline = baseline_pointer(history)
    if not baseline:
        fail(f"Production has never run {BASELINE}: run scripts/demos/rag/setup.py --only release first.")
    for environment in ("staging", "production"):
        current = rag.read_pointer(environment)
        if current and (current["version"], current["sha256"], current["index"]) == \
                (baseline["version"], baseline["sha256"], baseline["index"]):
            print(f"  {environment}: on {BASELINE}")
            continue
        reset.act(f"point {environment} at {BASELINE} (now {(current or {}).get('version') or 'nothing'})",
                  lambda environment=environment: rag.write_pointer(
                      environment, {**baseline, "environment": environment, "at": time.strftime(
                          "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "build": "reset"}))


def step_stage(reset, shared, version):
    current = host.main_version(rag.config()["path"])
    if current == version:
        print(f"  main has {rag.SERVICE} {version}")
        return
    reset.act(f"stage {rag.SERVICE} {version} on main (main has {current})", lambda: host.stage(rag.SERVICE, version))


def step_search(reset, shared):
    from databricks_api import Databricks

    db = Databricks.as_pipeline()
    settings = rag.databricks_settings()
    endpoint = db.endpoint(settings["endpoint"])
    if ((endpoint or {}).get("endpoint_status") or {}).get("state") != "ONLINE":
        fail(UP_FIRST)
    pointer = rag.read_pointer("production")
    index = db.index(pointer["index"]) if pointer else None
    if not ((index or {}).get("status") or {}).get("ready"):
        fail(UP_FIRST)
    print(f"  endpoint {settings['endpoint']} online, {pointer['index'].rsplit('.', 1)[1]} online")


def step_warm(reset, shared):
    import answer

    if reset.dry_run:
        print("  would ask production one smoke question")
        return
    question = rag.smoke_questions(1)[0]
    found = answer.answer(question["question"], "production")
    print(f"  production ({found['version']}) answered {question['id']} from "
          f"{', '.join(s['id'] for s in found['sources']) or 'nothing'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--stage", default="2.1.0", choices=["2.1.0", "2.1.1"], help="the version to stage")
    parser.add_argument("--only", default=",".join(STEPS), help=f"comma-separated, from: {', '.join(STEPS)}")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        return host.hand_over("reset-rag", "demos/rag/reset.py",
                              [*(["--dry-run"] if args.dry_run else []), "--stage", args.stage, "--only", args.only],
                              title=f"reset.py rag{' --dry-run' if args.dry_run else ''}")
    sys.stdout.reconfigure(line_buffering=True)
    started = time.monotonic()
    shared = main_reset([take_issue()])
    reset = shared.Reset(args.dry_run)
    print(f"Reset demo 23: {rag.SERVICE} {BASELINE} in staging and production, {args.stage} staged"
          f"{' (dry run: nothing changes)' if args.dry_run else ''}.")
    for name in [s for s in STEPS if s in args.only.split(",")]:
        body = (lambda: step_stage(reset, shared, args.stage)) if name == "stage" else \
            (lambda name=name: globals()[f"step_{name}"](reset, shared))
        reset.step(name, body)
    took = time.monotonic() - started
    if reset.failed:
        print(f"Not everything reset after {took:.0f}s: {', '.join(reset.failed)}. The messages above say why.")
        return 1
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s). Ready for a take.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
