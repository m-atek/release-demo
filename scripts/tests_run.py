"""The Test stage with one rerun in place and a quarantine (demo 15): a service's scripts.test.

    python scripts/tests_run.py orders-service

Runs the service's pytest suite with JUnit and coverage, as the pipeline's
default test step does, then reruns only the failed tests, once, in the same
workspace and container, into their own JUnit file:

- failed, then passed: flaky, and the stage passes;
- failed twice: a real failure, and the stage fails;
- a suite with no failures behaves exactly as before.

junit.xml keeps each test's final result; the attempts go to
junit-attempts.xml, which exists only when a rerun happened. Tests in the
quarantine file (services.yaml, tests.quarantine) run, but their failures
don't fail the stage and they aren't rerun. An entry whose until date has
passed fails the stage at its start, before any test runs.

Runs in the toolchain container, so it calls nothing outside it: the events
and Slack's lines come after it, on the host (events.py tests, flaky.py lines).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import junit_results
import quarantine
from common import ROOT, service

SCRIPTS = Path(__file__).resolve().parent
REPORTS = Path(os.environ.get("WORKSPACE", ROOT)) / ".release" / "reports"


def pytest_command(*args):
    return [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
            "-p", "pytest_quarantine", *args]


def environment(entries, coverage_file):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SCRIPTS), env.get("PYTHONPATH")]))
    env["QUARANTINE"] = json.dumps([{"test": e["test"], "issue": e["issue"], "until": str(e["until"])} for e in entries])
    env["COVERAGE_FILE"] = coverage_file
    return env


def collect(directory):
    """The node ids the suite has, without running it."""
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q"],
                            cwd=directory, capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if "::" in line]


def run(command, directory, env):
    """Run pytest, its output into the log as it comes. Returns (exit code, seconds)."""
    started = time.monotonic()
    code = subprocess.run(command, cwd=directory, env=env).returncode
    return code, round(time.monotonic() - started, 3)


def failed_tests(path):
    root = ET.parse(path).getroot()
    return [junit_results.node_id(case) for case in root.iter("testcase") if junit_results.status(case) == "failed"]


def merge(final_path, rerun_path, attempts_path, first_seconds, rerun_seconds):
    """junit.xml with each rerun test's final result, and the attempts file."""
    final = ET.parse(final_path)
    rerun = {junit_results.node_id(case): case for case in ET.parse(rerun_path).getroot().iter("testcase")}
    attempts = ET.Element("testsuites", name="attempts")
    first_suite = ET.SubElement(attempts, "testsuite", name="first pass", time=f"{first_seconds:.3f}")
    rerun_suite = ET.SubElement(attempts, "testsuite", name="rerun", time=f"{rerun_seconds:.3f}")
    for suite in final.getroot().iter("testsuite"):
        for index, case in enumerate(list(suite)):
            test = junit_results.node_id(case) if case.tag == "testcase" else None
            if test in rerun:
                first_suite.append(case)
                rerun_suite.append(rerun[test])
                suite.remove(case)
                suite.insert(index, rerun[test])
        failures = sum(1 for case in suite.iter("testcase") if case.find("failure") is not None)
        errors = sum(1 for case in suite.iter("testcase") if case.find("error") is not None)
        suite.set("failures", str(failures))
        suite.set("errors", str(errors))
    for suite in attempts:
        suite.set("tests", str(len(suite)))
    final.getroot().set("time", f"{first_seconds:.3f}")
    final.write(final_path, encoding="utf-8", xml_declaration=True)
    ET.ElementTree(attempts).write(attempts_path, encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    args = parser.parse_args()

    config = service(args.service)
    settings = config.get("tests") or {}
    directory = config["dir"]
    REPORTS.mkdir(parents=True, exist_ok=True)
    final_path, attempts_path = REPORTS / junit_results.FINAL, REPORTS / junit_results.ATTEMPTS
    for stale in (final_path, attempts_path):
        stale.unlink(missing_ok=True)

    entries = []
    if settings.get("quarantine"):
        entries = quarantine.applying(quarantine.load(ROOT / settings["quarantine"]["file"]), args.service,
                                      collect(directory))
    ended = [entry for entry in entries if quarantine.ended(entry)]
    (REPORTS / "quarantine.json").write_text(json.dumps(
        {"applied": [{**e, "since": str(e["since"]), "until": str(e["until"])} for e in entries if e not in ended],
         "ended": [quarantine.ended_line(e) for e in ended]}, indent=2) + "\n")
    if ended:
        for entry in ended:
            print(quarantine.ended_line(entry))
        return 1
    for entry in entries:
        print(f"Quarantined until {quarantine.spoken(entry['until'])} ({entry['issue']}): {entry['test']}. "
              "It runs, but its failure doesn't fail this stage.")

    with tempfile.TemporaryDirectory() as temp:
        env = environment(entries, str(Path(temp) / "coverage"))
        code, first_seconds = run(pytest_command("-q", f"--junitxml={final_path}", "--cov=.",
                                                 f"--cov-report=xml:{REPORTS / 'coverage.xml'}"), directory, env)
        if code not in (0, 1) or not final_path.exists():
            return code or 1
        failed = failed_tests(final_path)
        if not failed:
            tree = ET.parse(final_path)
            tree.getroot().set("time", f"{first_seconds:.3f}")
            tree.write(final_path, encoding="utf-8", xml_declaration=True)
            return 0
        if int(settings.get("rerun_failed", 0)) < 1:
            return 1
        print(f"\nRerunning {len(failed)} failed test{'s' if len(failed) > 1 else ''} once, in place:", flush=True)
        rerun_path = Path(temp) / "junit-rerun.xml"
        _, rerun_seconds = run(pytest_command("-v", f"--junitxml={rerun_path}", "--cov=.", "--cov-report=", *failed),
                               directory, env)
        merge(final_path, rerun_path, attempts_path, first_seconds, rerun_seconds)

    results = junit_results.read(REPORTS)
    for line in junit_results.lines(results):
        print(line)
    return 1 if any(test["outcome"] == "failed" for test in results["tests"]) else 0


if __name__ == "__main__":
    sys.exit(main())
