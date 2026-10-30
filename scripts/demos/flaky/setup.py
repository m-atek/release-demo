"""Demo 15, once: the Jira story the two releases are for, and the dashboard.

    python scripts/demos/flaky/setup.py

Makes ORD-[r], "Show the delivery estimate per region", as the next free key
(jira_seed.py ends at ORD-90 and Jira gives keys in order), unless it exists
already, and prints its key for the script's placeholders. Then it puts the
dashboard "Flaky tests" on the Grafana stack. Safe to run again.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from jira import PROJECT, Jira  # noqa: E402

SUMMARY = "Show the delivery estimate per region"


def story(jira):
    """The story's key, made once."""
    found = jira.search(f'project = {PROJECT} AND summary ~ "\\"{SUMMARY}\\"" ORDER BY key', fields="summary")
    for issue in found:
        if issue["fields"]["summary"] == SUMMARY:
            return issue["key"], False
    return jira.create("Story", SUMMARY, "Each store's orders show the delivery estimate of its region, on the page "
                       "and in the API.", labels=["orders-service"]), True


def main():
    key, made = story(Jira())
    print(f"{key}: {SUMMARY} ({'made now' if made else 'there already'}). It is ORD-[r] in the script.")
    return subprocess.run([sys.executable, str(HERE / "dashboard.py")]).returncode


if __name__ == "__main__":
    sys.exit(main())
