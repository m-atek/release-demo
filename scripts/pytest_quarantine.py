"""A pytest plugin for the quarantine (demo 15): a quarantined test runs, but can't fail the session.

    QUARANTINE='[{"test": "tests/test_handler.py::test_x", "issue": "ORD-95", "until": "2026-11-13"}]' \\
        python -m pytest -p pytest_quarantine ...

scripts/tests_run.py loads it with the entries that apply to the version under
test. Each quarantined test is marked as an expected failure that may also
pass (xfail, not strict): its failure shows as xfailed and doesn't fail the
run, its pass as passed. Both carry the properties quarantine and
quarantine_until in the JUnit report (junit_family xunit1), so the results
say which tests were quarantined.
"""

import json
import os

import pytest


def entries():
    return {entry["test"].split("[", 1)[0]: entry for entry in json.loads(os.environ.get("QUARANTINE") or "[]")}


def pytest_collection_modifyitems(config, items):
    quarantined = entries()
    for item in items:
        entry = quarantined.get(item.nodeid.split("[", 1)[0])
        if not entry:
            continue
        item.add_marker(pytest.mark.xfail(reason=f"quarantined until {entry['until']} ({entry['issue']})", strict=False))
        item.user_properties += [("quarantine", entry["issue"]), ("quarantine_until", str(entry["until"]))]
