"""Put the Store fleet dashboard on the Grafana Cloud stack, then check its queries.

    python scripts/demos/windows/grafana.py            save it when it changed, then check
    python scripts/demos/windows/grafana.py --check    only the check

grafana/store-fleet.json: the wave progress, and below it one tile per
server, two columns and three rows, with the version above the status
(waiting, upgrading, healthy, failed, rolled back) in type of 40 px and more,
so it reads in a 960 px browser pane at 125%. That pane is 768 px wide to the
page: side by side, "upgrading" wouldn't fit half a tile, so each tile and
the progress stack their two lines. The progress leaves the pane's top right
free, where the videos put the face bubble. All of it fits the pane's height
in the kiosk view with the time controls hidden:
/d/release-demo-store-fleet/store-fleet?kiosk=true&_dash.hideTimePicker=true
It refreshes every 10 seconds. The rows follow the waves: STORE-001 and
STORE-006 (the one-server waves), STORE-002 and STORE-003, STORE-004 and
STORE-005. The data comes from fleet_events.py.

scripts/grafana_setup.py saves it with the other dashboards in grafana/; this
saves and checks it alone. Uses grafana_setup.py's client, folder and check,
with the pipeline's Editor token or the setup token. Prints no token.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import grafana_setup  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402

DASHBOARD = HERE.parents[2] / "grafana" / "store-fleet.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only run the panels' queries")
    args = parser.parse_args()

    ssm = ssm_client()
    token = (grafana_setup.optional_parameter(ssm, "grafana/setup-token")
             or get_parameter(ssm, "grafana/service-account-token"))
    grafana = grafana_setup.Grafana(get_parameter(ssm, "grafana/stack-url"), token)
    dashboard = json.loads(DASHBOARD.read_text())
    if not args.check:
        grafana_setup.ensure_folder(grafana)
        grafana_setup.ensure_dashboard(grafana, dashboard)
    failures = grafana_setup.check(grafana, dashboard)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
