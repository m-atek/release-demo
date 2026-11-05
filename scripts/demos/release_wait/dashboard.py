"""Put video 31's dashboard "Where a release waits" on the Grafana stack, or update it, or print its address for one run.

    python scripts/demos/release_wait/dashboard.py
    python scripts/demos/release_wait/dashboard.py --plain     the seconds as the events hold them, if Loki's ceil won't draw
    python scripts/demos/release_wait/dashboard.py --url 112   the page for build #112, printed

dashboard.json, next to this script: one line of text and one bar gauge, one bar per
stage of one release run, in the pipeline's order, on one scale, from the events
scripts/events.py already sends to Grafana Cloud Logs. So it reads any run in the
last 14 days, which is as long as those logs are kept. It goes in the Releases
dashboard's folder, "Release demo". Uses the pipeline's service account token from
SSM (Editor role). Safe to run again: it overwrites the dashboard with the same uid.
"""

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parents[1]))

from common import fail, get_parameter, ssm_client  # noqa: E402

FOLDER_UID = "release-demo-dashboards"  # scripts/grafana_setup.py makes it
UID = "release-demo-release-wait"
ROUNDED_UP = "| label_format seconds=`{{ ceil .duration_seconds }}` | unwrap seconds"
AS_SENT = "| unwrap duration_seconds"


def build(plain=False):
    """dashboard.json, with the seconds as the events hold them if asked: Grafana then rounds them down, Slack up."""
    dashboard = copy.deepcopy(json.loads((HERE / "dashboard.json").read_text()))
    if plain:
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                target["expr"] = target["expr"].replace(ROUNDED_UP, AS_SENT)
    return dashboard


def address(stack, number):
    """The page for one run. Its 14 days hold any run the logs still have, so nothing needs pinning."""
    return f"{stack.rstrip('/')}/d/{UID}?orgId=1&var-build={number}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plain", action="store_true", help="the seconds as the events hold them")
    parser.add_argument("--url", metavar="BUILD", help="print the page for this build, and change nothing")
    args = parser.parse_args()
    ssm = ssm_client()
    stack = get_parameter(ssm, "grafana/stack-url").rstrip("/")
    if args.url:
        print(address(stack, args.url))
        return 0
    dashboard = build(args.plain)
    request = urllib.request.Request(
        f"{stack}/api/dashboards/db", method="POST",
        data=json.dumps({"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": True,
                         "message": "From scripts/demos/release_wait/dashboard.json"}).encode(),
        headers={"Authorization": f"Bearer {get_parameter(ssm, 'grafana/service-account-token')}",
                 "Content-Type": "application/json", "User-Agent": "release-demo/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as error:
        fail(f"Grafana refused the dashboard ({error.code}): {error.read().decode()[:300]}")
    print(f"{dashboard['title']}: {reply['status']}, version {reply['version']}")
    print(f"  {stack}{reply['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
