"""Stage 2, Test: run inventory's JUnit tests with Maven.

    python scripts/demos/linux/test.py inventory

Runs mvn test in jenkins/toolchain/java.Dockerfile. Surefire writes one report
per test class; they are merged into .release/reports/junit.xml for Jenkins
and the change, and JaCoCo's coverage goes to .release/reports/jacoco.xml for
SonarQube Cloud. Exits non-zero when a test fails, after writing the reports.
"""

import argparse
import shutil
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import run_state  # noqa: E402
from common import service  # noqa: E402
from maven import mvn  # noqa: E402


def merge_reports(directory, destination):
    """Every TEST-*.xml under directory as one <testsuites>. Returns (tests, failures)."""
    suites = ElementTree.Element("testsuites")
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for path in sorted(Path(directory).glob("TEST-*.xml")):
        suite = ElementTree.parse(path).getroot()
        for key in totals:
            totals[key] += int(suite.get(key, 0))
        suites.append(suite)
    for key, value in totals.items():
        suites.set(key, str(value))
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    ElementTree.ElementTree(suites).write(destination, encoding="utf-8", xml_declaration=True)
    return totals["tests"], totals["failures"] + totals["errors"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    args = parser.parse_args()

    source = service(args.service)["dir"]
    code = mvn(source, "test")
    tests, failed = merge_reports(source / "target" / "surefire-reports", run_state.REPORTS / "junit.xml")
    coverage = source / "target" / "site" / "jacoco" / "jacoco.xml"
    if coverage.exists():
        shutil.copy2(coverage, run_state.REPORTS / "jacoco.xml")
    print(f"{tests} tests, {failed} failed. Reports: {run_state.REPORTS}")
    if code != 0 or failed or tests == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
