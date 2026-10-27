"""Put demo 4's Grafana dashboard on the stack, or update it.

    python scripts/demos/apis/dashboard.py

dashboard.json, next to this script: the pricing-api row with Request share,
Error rate and p95 latency per version from the load generator's data, the
CloudWatch errors the alarm reads, and the canary window and alarm marked as
annotations. Uses the service account token from SSM (Editor role). Safe to
run again: it overwrites the dashboard with the same uid.
"""

import json
import sys
import urllib.error
import urllib.request

import pricing
from common import fail, get_parameter, ssm_client


def main():
    dashboard = json.loads((pricing.HERE / "dashboard.json").read_text())
    ssm = ssm_client()
    stack = get_parameter(ssm, "grafana/stack-url").rstrip("/")
    request = urllib.request.Request(
        f"{stack}/api/dashboards/db", method="POST",
        data=json.dumps({"dashboard": dashboard, "overwrite": True,
                         "message": "From scripts/demos/apis/dashboard.json"}).encode(),
        headers={"Authorization": f"Bearer {get_parameter(ssm, 'grafana/service-account-token')}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as error:
        fail(f"Grafana refused the dashboard ({error.code}): {error.read().decode()[:300]}")
    print(f"{dashboard['title']}: {reply['status']}, version {reply['version']}")
    print(f"  {stack}{reply['url']}")


if __name__ == "__main__":
    sys.exit(main())
