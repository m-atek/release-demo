"""The audit log on screen in video 26, the terminal's audit-log.

    who.py peek       the events of peek and of the Secret it mounts
    who.py catalog    the writes to catalog's Deployment

Asks the cluster's audit log, through SSM, for production since the reset's
mark (the time reset.py stored on this Mac). It keeps the first event per
user, verb and object, and prints two lines an event, at most 56 columns, in
Europe/Amsterdam time. It prints the log's own time, code, verb, object and
user, and nothing else, and shortens one thing:
system:serviceaccount:kube-system: to kube-system:. About 5 seconds.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import remote  # noqa: E402
from common import fail  # noqa: E402

MARK = Path.home() / ".kube" / "release-demo" / "can-i-mark"
ZONE = ZoneInfo("Europe/Amsterdam")
WRITES = {"create", "update", "patch", "delete"}
KUBE_SYSTEM = "system:serviceaccount:kube-system:"


def read_mark(path=MARK):
    """Never a guess: without the mark, rows of an earlier take would pass for this one's."""
    if not path.exists():
        fail("The reset's mark is missing. Run the reset: .venv/bin/python scripts/reset.py can-i")
    return path.read_text().strip()


def store_mark(mark, path=MARK):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(mark + "\n")


def events(name, mark):
    """The rows k3s.py audit prints, as dictionaries."""
    code, output = remote.run(f"{remote.K3S} audit --since {mark} --namespace production --name {name}", timeout=60)
    if code != 0:
        fail(f"The cluster's audit log didn't answer: {output.strip()}")
    rows = []
    for line in output.splitlines():
        if line.startswith("{"):
            rows.append(json.loads(line))
    return rows


def wanted(row, name):
    """For catalog, only writes to the Deployment, so the rolling pods don't fill the screen."""
    if name != "catalog":
        return True
    return row.get("resource") == "deployments" and row.get("name") == "catalog" and row.get("verb") in WRITES


def keep(rows, name):
    """The first event per user, verb and object, in the order they were asked."""
    seen, kept = set(), []
    for row in sorted(rows, key=lambda r: r.get("time") or ""):
        key = (row.get("user"), row.get("verb"), row.get("resource"), row.get("subresource"), row.get("name"))
        if key not in seen and wanted(row, name):
            seen.add(key)
            kept.append(row)
    return kept


def clock(time):
    moment = datetime.datetime.fromisoformat(time.replace("Z", "+00:00"))
    return moment.astimezone(ZONE).strftime("%H:%M:%S")


def target(row):
    """pods/peek-5b7c9d6f4-x2x9q/log, as the event names it."""
    return "/".join(part for part in (row.get("resource"), row.get("name"), row.get("subresource")) if part)


def user(row):
    name = row.get("user") or ""
    return "kube-system:" + name.removeprefix(KUBE_SYSTEM) if name.startswith(KUBE_SYSTEM) else name


def lines(rows):
    printed = []
    for row in rows:
        printed.append(f"{clock(row['time'])}  {row.get('code') or '---'}  {row.get('verb')} {target(row)}")
        printed.append(f"    by {user(row)}")
    return printed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", choices=["peek", "catalog"])
    args = parser.parse_args()
    rows = keep(events(args.name, read_mark()), args.name)
    print("\n".join(lines(rows)) if rows else "Nothing in the audit log since the reset.")


if __name__ == "__main__":
    main()
