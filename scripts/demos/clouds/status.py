"""What demo 5 runs now, and what it costs.

    python scripts/demos/clouds/status.py           each target's version, from its /health
    python scripts/demos/clouds/status.py --wait    wait until every target that is up answers, then Ready

Reads the addresses from SSM (clouds/aws, clouds/azure, clouds/gcp), so it
needs no cloud login.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

from common import http_get, service, ssm_client  # noqa: E402

COSTS = {
    "aws": "Lambda and ECR: cents a month at demo volumes, nothing while idle",
    "azure": "Container Registry Basic about USD 0.17 a day while up; Container Apps inside the free grant",
    "gcp": "Cloud Run inside the free grant; Artifact Registry free up to 0.5 GB",
}
SET_UP = {"aws": "scripts/demos/clouds/bootstrap.sh aws", "azure": "scripts/demos/clouds/bootstrap.sh azure",
          "gcp": "scripts/demos/clouds/bootstrap.sh gcp"}


def settings(ssm, cloud):
    try:
        return json.loads(ssm.get_parameter(Name=f"/release-demo/clouds/{cloud}")["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return None


def version(url):
    status, body = http_get(f"{url}/health", timeout=10)
    if status != 200:
        return None
    try:
        return json.loads(body).get("version")
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    ssm = ssm_client()
    config = service("quote-service")
    waiting = []
    for cloud in config["clouds"]:
        found = settings(ssm, cloud["key"])
        if found is None:
            print(f"{cloud['name']}: not set up yet ({SET_UP[cloud['key']]})")
            continue
        urls = found.get("urls") or {}
        if not urls:
            print(f"{cloud['name']}: down, registry and apps deleted, nothing billed (scripts/demo.sh up clouds)")
            continue
        print(f"{cloud['name']}: up. {COSTS[cloud['key']]}")
        for environment in config["environments"]:
            url = urls.get(environment)
            if not url:
                continue
            seen = version(url)
            print(f"  {environment:<10} {seen or 'not answering':<14} {url}")
            if not seen:
                waiting.append((cloud["name"], environment, url))

    if not args.wait:
        return
    deadline = time.monotonic() + 300
    while waiting and time.monotonic() < deadline:
        time.sleep(5)
        waiting = [w for w in waiting if not version(w[2])]
    if waiting:
        print("Not answering after 5 minutes: " + ", ".join(f"{c} {e}" for c, e, _ in waiting))
        raise SystemExit(1)
    print("Ready")


if __name__ == "__main__":
    main()
