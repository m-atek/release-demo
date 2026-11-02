"""Video 25, once: the Jira issue both releases go out under.

    python scripts/demos/database-blip/setup.py

Makes ORD-[b], "Read the catalog from Postgres", as the next free key, unless
it exists already, and prints its key for the script's placeholders. Safe to
run again.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from jira import PROJECT, Jira  # noqa: E402

SUMMARY = "Read the catalog from Postgres"
DESCRIPTION = "catalog reads its products from a Postgres database instead of a file."


def issue(jira):
    """The issue's key, made once."""
    found = jira.search(f'project = {PROJECT} AND summary ~ "\\"{SUMMARY}\\"" ORDER BY key', fields="summary")
    for item in found:
        if item["fields"]["summary"] == SUMMARY:
            return item["key"], False
    return jira.create("Story", SUMMARY, DESCRIPTION, labels=["catalog"]), True


def main():
    key, made = issue(Jira())
    print(f"{key}: {SUMMARY} ({'made now' if made else 'there already'}). It is ORD-[b] in the script.")


if __name__ == "__main__":
    main()
