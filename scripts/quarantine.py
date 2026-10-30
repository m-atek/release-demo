"""The quarantine file (demo 15): tests whose failures don't stop a release, each until its end date.

    import quarantine
    entries = quarantine.load(path)                 # [{service, test, issue, since, until, reason}]
    path.write_text(quarantine.render(entries))     # the same header, the entries in order

tests/quarantine.yaml at the repository root, outside the service folders,
so staging another version keeps it. scripts/flaky.py decide adds an entry;
a person removes one with the fix, or extends its until date. An entry
applies only to a test the staged version has. From the day after until, it
fails the Test stage at its start. Dates are Europe/Amsterdam's.
"""

import datetime
from pathlib import Path

import yaml

HEADER = """\
# Tests whose failures don't stop a release, each until its end date.
#
# The release pipeline adds an entry, with a Jira issue due on the until date,
# when a test of the version it releases failed and then passed on rerun often
# enough (services.yaml, tests.quarantine): scripts/flaky.py decide. The test
# still runs and its result is recorded, but its failure doesn't fail the Test
# stage, and it isn't rerun. From the day after until, the entry fails the Test
# stage at its start: fix the test and remove the entry, or extend it.
"""
FIELDS = ["service", "test", "issue", "since", "until", "reason"]


def today():
    """The date in Europe/Amsterdam, where the demo runs; UTC where no time zone data is installed."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc).date()


def as_date(value):
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def spoken(day):
    """13 Nov 2026, as Slack writes a date."""
    day = as_date(day)
    return f"{day.day} {day:%b %Y}"


def load(path):
    path = Path(path)
    return load_text(path.read_text()) if path.exists() else []


def load_text(text):
    entries = (yaml.safe_load(text) or {}).get("quarantine") or []
    for entry in entries:
        entry["since"], entry["until"] = as_date(entry["since"]), as_date(entry["until"])
    return entries


def render(entries):
    if not entries:
        return HEADER + "\nquarantine: []\n"
    lines = [HEADER, "quarantine:"]
    for entry in entries:
        for index, field in enumerate(FIELDS):
            value = entry[field]
            value = value.isoformat() if isinstance(value, datetime.date) else value
            text = value if field in ("since", "until") else yaml.safe_dump(value, default_flow_style=True,
                                                                            width=1000).strip().removesuffix("...").strip()
            lines.append(f"  {'- ' if index == 0 else '  '}{field}: {text}")
    return "\n".join(lines) + "\n"


def base(test):
    """A node id without its parameters: tests/x.py::test_y[a] is tests/x.py::test_y."""
    return test.split("[", 1)[0]


def applying(entries, service, tests):
    """The entries for this service whose test the version has."""
    have = {base(test) for test in tests}
    return [entry for entry in entries if entry["service"] == service and base(entry["test"]) in have]


def ended(entry, day=None):
    return (day or today()) > as_date(entry["until"])


def ended_line(entry):
    test = entry["test"].rsplit("::", 1)[-1]
    return f"Quarantine ended on {spoken(entry['until'])} for {test} ({entry['issue']}). Fix it or extend it."
