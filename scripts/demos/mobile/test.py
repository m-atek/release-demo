"""Stage 2, Test, for store-floor: flutter analyze and flutter test, in the pinned container.

    python scripts/demos/mobile/test.py store-floor

Writes .release/reports/junit.xml from Flutter's JSON test report, and
.release/reports/lcov.info with paths from the repository root, which is where
SonarQube Cloud reads Dart coverage from. Exits 1 on a failed test or an
analyzer error or warning.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import mobile
from mobile import ROOT, SERVICE


def junit(events, root=""):
    """A JUnit report from `flutter test --file-reporter json:...` events."""
    suites, tests, results, errors = {}, {}, {}, {}
    for event in events:
        kind = event.get("type")
        if kind == "suite":
            suites[event["suite"]["id"]] = event["suite"]["path"]
        elif kind == "testStart":
            tests[event["test"]["id"]] = {**event["test"], "start": event["time"]}
        elif kind == "testDone":
            results[event["testID"]] = event
        elif kind == "error":
            errors.setdefault(event["testID"], []).append(f"{event['error']}\n{event.get('stackTrace', '')}")
    testsuites = ET.Element("testsuites")
    by_suite = {}
    for test_id, test in tests.items():
        done = results.get(test_id, {})
        if done.get("hidden"):
            continue  # Flutter's "loading <file>" pseudo-tests
        by_suite.setdefault(test["suiteID"], []).append((test, done))
    failed = 0
    for suite_id, cases in sorted(by_suite.items()):
        path = suites.get(suite_id, "tests").removeprefix(root).lstrip("/")
        suite = ET.SubElement(testsuites, "testsuite", name=path, tests=str(len(cases)))
        failures = 0
        for test, done in cases:
            seconds = max(0, done.get("time", test["start"]) - test["start"]) / 1000
            case = ET.SubElement(suite, "testcase", classname=path.replace("/", ".").removesuffix(".dart"),
                                 name=test["name"], time=f"{seconds:.3f}")
            result = done.get("result", "error")
            if done.get("skipped"):
                ET.SubElement(case, "skipped")
            elif result != "success":
                failures += 1
                failure = ET.SubElement(case, "failure", message=result)
                failure.text = "\n".join(errors.get(test["id"], [])) or result
        suite.set("failures", str(failures))
        failed += failures
    return ET.tostring(testsuites, encoding="unicode"), failed


def rebase_lcov(text, prefix):
    """lcov.info names files from the app folder; SonarQube wants the repository root."""
    return "".join(f"SF:{prefix}/{line[3:]}" if line.startswith("SF:") and not line.startswith("SF:/") else line
                   for line in text.splitlines(keepends=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", choices=[SERVICE])
    parser.parse_args()

    import flutter

    config = mobile.config()
    source = config["dir"]
    reports = Path(os.environ.get("WORKSPACE", ROOT)) / ".release" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    raw = source / "build" / "test-report.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.unlink(missing_ok=True)

    analyzed = flutter.in_container("flutter pub get > /dev/null && flutter analyze --no-fatal-infos", cwd=source,
                                    check=False).returncode
    tested = flutter.in_container(f"flutter test --coverage --reporter expanded --file-reporter json:{raw}",
                                  cwd=source, check=False).returncode
    events = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()] if raw.exists() else []
    report, failed = junit(events, root=str(source))
    (reports / "junit.xml").write_text(report)
    lcov = source / "coverage" / "lcov.info"
    if lcov.exists():
        (reports / "lcov.info").write_text(rebase_lcov(lcov.read_text(), config["path"]))
    print(f"Reports: .release/reports/junit.xml{' and lcov.info' if lcov.exists() else ''}")
    if analyzed:
        print("flutter analyze found errors or warnings")
    if tested or failed or not events:
        print(f"{failed} failed test{'s' if failed != 1 else ''}" if events else "flutter test wrote no report")
    sys.exit(1 if analyzed or tested or failed or not events else 0)


if __name__ == "__main__":
    main()
