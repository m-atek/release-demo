"""Video 27, once: the Jira stories of its releases, and the dashboard.

    python scripts/demos/error_budget/setup.py              from the Mac, after the applies
    python scripts/demos/error_budget/setup.py --dry-run    what it would make, and nothing else

Makes three Stories in ORD as Release Pipeline, the next free keys, unless they
exist, and prints their keys for the script's placeholders: ORD-[p] "Add a price
lookup to the API", ORD-[a] "Accept product names from the old store app" and
ORD-[f] "Show the pickup time on the order page", each with its fix versions.
ORD-[b] isn't here: the budget opens it, in the take. Then it puts the dashboard
"Error budget" on the Grafana stack. Safe to run again.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parents[1]))

SERVICE = "orders-service"
STORIES = {
    "p": ("Add a price lookup to the API", ["1.26.0"], "/api/price?product=<name> answers a product's price in euros."),
    "a": ("Accept product names from the old store app", ["1.27.0"],
          "The old store app sends a product name with its variant after a comma, such as \"Rain jacket, blue\"."),
    "f": ("Show the pickup time on the order page", ["1.28.0", "1.28.1"],
          "The order page says how soon an order is ready for pickup in store."),
}


def find_story(jira, summary):
    """A story's key, found by its exact summary."""
    from jira import PROJECT

    found = jira.search(f'project = {PROJECT} AND summary ~ "\\"{summary}\\"" ORDER BY key', fields="summary")
    return next((issue["key"] for issue in found if issue["fields"]["summary"] == summary), None)


def story_keys(jira):
    """{letter: key} of the stories, for reset.py. Missing means setup.py hasn't run."""
    keys = {letter: find_story(jira, story[0]) for letter, story in STORIES.items()}
    missing = [STORIES[letter][0] for letter, key in keys.items() if not key]
    if missing:
        raise RuntimeError(f"No Jira story {', '.join(missing)} yet: run scripts/demos/error_budget/setup.py once.")
    return keys


def stories(jira, dry_run=False):
    for letter, (summary, versions, description) in STORIES.items():
        key = find_story(jira, summary)
        made = key is None
        if made and dry_run:
            print(f"ORD-[{letter}]: would make \"{summary}\" with the next free key, in {', '.join(versions)}")
            continue
        if made:
            key = jira.create("Story", summary, description, labels=[SERVICE])
        print(f"ORD-[{letter}] is {key}: {summary} ({'made now' if made else 'there already'})")
        if dry_run:
            continue
        for version in versions:
            found = jira.version(f"{SERVICE} {version}")
            jira.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"fixVersions": [{"add": {"id": found["id"]}}]}})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from jira import Jira

    stories(Jira(), args.dry_run)
    if args.dry_run:
        print("Grafana: would put the dashboard \"Error budget\" on the stack")
        return 0
    return subprocess.run([sys.executable, str(HERE / "dashboard.py")]).returncode


if __name__ == "__main__":
    sys.exit(main())
