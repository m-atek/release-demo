"""Put video 27's dashboard "Error budget" on the Grafana stack, or update it.

    python scripts/demos/error_budget/dashboard.py
    python scripts/demos/error_budget/dashboard.py --budget-line 19     the dashed line, for the partly filled fallback
    python scripts/demos/error_budget/dashboard.py --per-minute         failed requests per minute as bars, if a running total won't draw

dashboard.json, next to this script: the four numbers, the running total of
failed requests with the release marks and a dashed budget line, the burn rate,
the generator's own count and the overrides. It goes in the Releases dashboard's
folder, "Release demo", default range the last 24 hours. The metrics' names come
from services.yaml's error_budget.metrics, as the stage reads them. Each panel's
description gives its formula. Uses the pipeline's service account token from
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

from common import fail, get_parameter, service, ssm_client  # noqa: E402

FOLDER_UID = "release-demo-dashboards"  # scripts/grafana_setup.py makes it
SERVICE = "orders-service"
CHART = "Failed requests, running total"
PER_MINUTE = "Failed requests per minute"
BUDGET_LINE = 86


def build(budget_line=BUDGET_LINE, per_minute=False, metrics=None):
    """dashboard.json with the metrics' names and the options applied."""
    dashboard = copy.deepcopy(json.loads((HERE / "dashboard.json").read_text()))
    metrics = metrics or service(SERVICE)["error_budget"]["metrics"]
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if target.get("label") in metrics and target.get("namespace") == "AWS/Lambda":
                target["metricName"] = metrics[target["label"]]
    chart = next(panel for panel in dashboard["panels"] if panel.get("title") == CHART)
    for target in chart["targets"]:
        if target.get("type") == "math":
            target["expression"] = f"$B * 0 + {budget_line}"
    for override in chart["fieldConfig"]["overrides"]:
        for prop in override["properties"]:
            if prop["id"] == "displayName" and str(prop["value"]).startswith("budget: "):
                prop["value"] = f"budget: {budget_line}"
    if per_minute:
        chart["title"] = PER_MINUTE
        chart["description"] = chart["description_per_minute"]
        chart["transformations"] = []
        chart["fieldConfig"]["defaults"]["custom"].update(drawStyle="bars", fillOpacity=80, lineWidth=1)
    chart.pop("description_per_minute", None)
    return dashboard


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget-line", type=int, default=BUDGET_LINE, help="the dashed line's number")
    parser.add_argument("--per-minute", action="store_true", help="bars per minute instead of the running total")
    args = parser.parse_args()
    dashboard = build(args.budget_line, args.per_minute)
    ssm = ssm_client()
    stack = get_parameter(ssm, "grafana/stack-url").rstrip("/")
    request = urllib.request.Request(
        f"{stack}/api/dashboards/db", method="POST",
        data=json.dumps({"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": True,
                         "message": "From scripts/demos/error_budget/dashboard.json"}).encode(),
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
