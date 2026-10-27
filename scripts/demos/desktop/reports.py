"""Stage 2, Test, on the tools host: one JUnit report from the agents' tests.

    python scripts/demos/desktop/reports.py store-assistant

The Mac agent's tests leave dist/<service>/parts/mac/reports/junit-mac.xml and
coverage-mac.xml (SonarQube's generic format); the Windows agent's leave a
.trx file and a Cobertura coverage file in parts/windows/reports/. This merges
the tests into .release/reports/junit.xml, the report the pipeline keeps and
attaches to the change, copies the coverage next to it for the quality gate,
and exits 1 when a test failed or a track has no tests at all.
"""

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import run_state  # noqa: E402
from common import ROOT, fail, service  # noqa: E402

TRX = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"


def seconds(duration):
    """A .trx duration, 00:00:00.0123456, in seconds."""
    hours, minutes, rest = (duration or "0:0:0").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def trx_suite(path, name):
    """A .trx file as a JUnit testsuite element."""
    root = ET.parse(path).getroot()
    suite = ET.Element("testsuite", name=name)
    count = failures = skipped = 0
    for result in root.iter(f"{TRX}UnitTestResult"):
        count += 1
        case = ET.SubElement(suite, "testcase", classname=name, name=result.get("testName", ""),
                             time=f"{seconds(result.get('duration')):.3f}")
        outcome = result.get("outcome")
        if outcome == "Failed":
            failures += 1
            message = result.findtext(f"{TRX}Output/{TRX}ErrorInfo/{TRX}Message") or "failed"
            failure = ET.SubElement(case, "failure", message=message.strip()[:500])
            failure.text = result.findtext(f"{TRX}Output/{TRX}ErrorInfo/{TRX}StackTrace") or ""
        elif outcome not in ("Passed",):
            skipped += 1
            ET.SubElement(case, "skipped")
    suite.set("tests", str(count))
    suite.set("failures", str(failures))
    suite.set("skipped", str(skipped))
    return suite


def junit_suites(path):
    root = ET.parse(path).getroot()
    return [root] if root.tag == "testsuite" else list(root.iter("testsuite"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    args = parser.parse_args()

    config = service(args.service)
    tracks = config.get("desktop", {}).get("tracks", ["mac", "windows"])
    reports = run_state.REPORTS
    reports.mkdir(parents=True, exist_ok=True)
    merged = ET.Element("testsuites")
    for track in tracks:
        directory = ROOT / "dist" / args.service / "parts" / track / "reports"
        suites = []
        for path in sorted(directory.glob("*.xml")):
            if path.name.startswith("junit"):
                suites += junit_suites(path)
            elif path.name.startswith("coverage"):
                shutil.copy2(path, reports / path.name)
        for path in sorted(directory.glob("*.trx")):
            suites.append(trx_suite(path, f"StoreAssistant ({track})"))
        tests = sum(int(s.get("tests", 0)) for s in suites)
        failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        print(f"{'ok  ' if tests and not failed else 'FAIL'} {track}: {tests} tests, {failed} failed")
        if not tests:
            fail(f"No test results from the {track} agent in {directory.relative_to(ROOT)}.")
        merged.extend(suites)
    ET.ElementTree(merged).write(reports / "junit.xml", encoding="utf-8", xml_declaration=True)
    failures = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in merged)
    print(f"Wrote {(reports / 'junit.xml').relative_to(run_state.PATH.parents[1])}")
    if failures:
        fail(f"{failures} tests failed.")


if __name__ == "__main__":
    main()
