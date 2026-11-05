"""Video 31, once: the Jira stories of its two releases, and the dashboard.

    python scripts/demos/release_wait/setup.py              from the Mac, after the merge
    python scripts/demos/release_wait/setup.py --dry-run    what it would make, and nothing else

Makes two Stories in ORD as Release Pipeline, the next free keys, unless they exist,
and prints their keys for the script's placeholders: ORD-[a] for 1.33.0, which waits
for Alex, and ORD-[b] for 1.33.1, pre-approved. The pipeline's own jira.py gives each
its fix version in the take. Then it puts the dashboard "Where a release waits" on
the Grafana stack. Safe to run again.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parents[1]))

SERVICE = "orders-service"
STORIES = {
    "a": ("Show the returns policy on the order page", "One line on the order page: free returns within 30 days."),
    "b": ("Show the order cut-off on the order page", "One line on the order page: order by 20:00, shipped today."),
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
        raise RuntimeError(f"No Jira story {', '.join(missing)} yet: run scripts/demos/release_wait/setup.py once.")
    return keys


def stories(jira, dry_run=False):
    for letter, (summary, description) in STORIES.items():
        key = find_story(jira, summary)
        if key is None and dry_run:
            print(f"ORD-[{letter}]: would make \"{summary}\" with the next free key")
            continue
        made = key is None
        if made:
            key = jira.create("Story", summary, description, labels=[SERVICE])
        print(f"ORD-[{letter}] is {key}: {summary} ({'made now' if made else 'there already'})")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from jira import Jira

    stories(Jira(), args.dry_run)
    if args.dry_run:
        print("Grafana: would put the dashboard \"Where a release waits\" on the stack")
        return 0
    return subprocess.run([sys.executable, str(HERE / "dashboard.py")]).returncode


if __name__ == "__main__":
    sys.exit(main())
