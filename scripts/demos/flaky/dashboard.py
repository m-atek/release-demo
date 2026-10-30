"""Put demo 15's dashboard "Flaky tests" on the Grafana stack, or update it.

    python scripts/demos/flaky/dashboard.py

dashboard.json, next to this script: the table "Flaky tests, last 14 days"
and the stats "Reruns avoided" and "Minutes saved", from the per-test events
(scripts/events.py tests), default range the last 14 days. It goes in the
Releases dashboard's folder, "Release demo". Each panel's description gives
its formula. Uses the pipeline's service account token from SSM (Editor
role). Safe to run again: it overwrites the dashboard with the same uid.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from common import fail, get_parameter, ssm_client  # noqa: E402

FOLDER_UID = "release-demo-dashboards"  # scripts/grafana_setup.py makes it


def main():
    dashboard = json.loads((HERE / "dashboard.json").read_text())
    ssm = ssm_client()
    stack = get_parameter(ssm, "grafana/stack-url").rstrip("/")
    request = urllib.request.Request(
        f"{stack}/api/dashboards/db", method="POST",
        data=json.dumps({"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": True,
                         "message": "From scripts/demos/flaky/dashboard.json"}).encode(),
        headers={"Authorization": f"Bearer {get_parameter(ssm, 'grafana/service-account-token')}",
                 "Content-Type": "application/json", "User-Agent": "release-demo/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as error:
        fail(f"Grafana refused the dashboard ({error.code}): {error.read().decode()[:300]}")
    print(f"{dashboard['title']}: {reply['status']}, version {reply['version']}")
    print(f"  {stack}{reply['url']}")


if __name__ == "__main__":
    sys.exit(main())
