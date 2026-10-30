"""Demo 21, once: the three Jira issues of 1.8.0, and their keys where the demo reads them.

    python scripts/demos/release-train/setup.py

Makes ORD-[u] and ORD-[v] (Done) and ORD-[w] (In Progress), the next free
keys, unless they exist, all three in the fix version "orders-service 1.8.0",
and prints the keys for the script's placeholders. It writes ORD-[w] into
1.8.0's flags.yaml, as the issue delivery_estimate_v2 hides, and the three
keys into the release calendar (demo-data/release-calendar.html): commit both.
Safe to run again.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parents[1]))

from common import ROOT  # noqa: E402

SERVICE, VERSION = "orders-service", "1.8.0"
ISSUES = {
    "u": ("Add the item count to each order in /api/orders", "Done",
          "Each order in /api/orders says how many items it has, for the store apps."),
    "v": ("Show order totals with a thousands separator", "Done", "The order page writes totals as 1,234.50."),
    "w": ("Delivery estimate per carrier", "In Progress",
          "The page and /api/estimate give the delivery estimate of each store's carrier."),
}
FLAGS = ROOT / "demo-states" / SERVICE / VERSION / "flags.yaml"
CALENDAR = ROOT / "demo-data" / "release-calendar.html"


def issue_keys(jira):
    from jira import PROJECT

    keys = {}
    for letter, (summary, status, description) in ISSUES.items():
        found = [i["key"] for i in jira.search(f'project = {PROJECT} AND summary ~ "\\"{summary}\\""', fields="summary")
                 if i["fields"]["summary"] == summary]
        key = found[0] if found else jira.create("Story", summary, description, labels=[SERVICE],
                                                 fix_version=f"{SERVICE} {VERSION}")
        jira.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"fixVersions": [
            {"add": {"id": jira.version(f"{SERVICE} {VERSION}")["id"]}}]}})
        jira.transition(key, status)
        keys[letter] = key
        print(f"ORD-[{letter}] is {key}: {summary}, {status}{'' if found else ' (made now)'}")
    return keys


def write_keys(keys):
    text = FLAGS.read_text()
    text = re.sub(r"^  issue: .*$", f"  issue: {keys['w']}", text, count=1, flags=re.MULTILINE)
    FLAGS.write_text(text)
    page = CALENDAR.read_text()
    for letter, key in keys.items():
        page = re.sub(rf"<td>ORD-(\[{letter}\]|\d+)</td><td>{re.escape(ISSUES[letter][0])}",
                      f"<td>{key}</td><td>{ISSUES[letter][0]}", page)
    CALENDAR.write_text(page)
    print(f"Wrote {keys['w']} into {FLAGS.relative_to(ROOT)}, and the keys into {CALENDAR.relative_to(ROOT)}: commit both.")


def main():
    from jira import Jira

    keys = issue_keys(Jira())
    write_keys(keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
