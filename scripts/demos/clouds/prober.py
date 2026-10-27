"""Grafana's view of quote-service: every target's /health, every 5 seconds, during a run.

    python scripts/demos/clouds/prober.py --follow .release/state.json   until that run ends (deploy.py starts it)
    python scripts/demos/clouds/prober.py --minutes 10                   on its own, from the tools host or the Mac

Each probe of each of the six targets is one line in Grafana Cloud Logs,
labelled with the cloud and environment: the version /health reports, the
HTTP status and the response time in milliseconds, measured from where the
prober runs (the tools host, in eu-west-1). Grafana shows the version per cloud
and environment, and the response time per cloud, from these lines.

The addresses come from SSM (clouds/aws, clouds/azure, clouds/gcp), so the
prober needs no cloud access. With --follow it stops when the run finishes
(the run state gets its total time), when another run replaces it, or after
an hour.
"""

import argparse
import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

from common import get_parameter, http_get, service, ssm_client  # noqa: E402
from events import send  # noqa: E402

INTERVAL = 5
MAX_SECONDS = 3600


def targets(config, ssm):
    """(cloud, environment, base URL) for every target whose cloud is set up and up."""
    found = []
    for cloud in config["clouds"]:
        try:
            settings = json.loads(ssm.get_parameter(Name=f"/release-demo/clouds/{cloud['key']}")["Parameter"]["Value"])
        except ssm.exceptions.ParameterNotFound:
            continue
        for environment, url in (settings.get("urls") or {}).items():
            if url:
                found.append((cloud["key"], environment, url.rstrip("/")))
    return found


def probe(target):
    cloud, environment, url = target
    started = time.monotonic()
    status, body = http_get(f"{url}/health", timeout=4)
    milliseconds = round((time.monotonic() - started) * 1000)
    version = None
    if status == 200:
        try:
            version = json.loads(body).get("version")
        except ValueError:
            pass
    return {"service": "quote-service", "cloud": cloud, "environment": environment, "version": version,
            "status": status, "ms": milliseconds, "at": int(time.time())}


def push(loki, results):
    """One push per round: a stream per target, labelled with its cloud and environment."""
    url, auth = loki
    now = str(time.time_ns())
    streams = [{"stream": {"source": "release-demo", "service": r["service"], "kind": "probe", "cloud": r["cloud"],
                           "environment": r["environment"]}, "values": [[now, json.dumps(r)]]} for r in results]
    return send(url, {"streams": streams}, {"Authorization": f"Basic {auth}"})


def run_over(state_path, build):
    """The followed run finished, or another run took its place."""
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return True
    return bool(state.get("total")) or str(state.get("build", {}).get("number")) != build


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", default="quote-service")
    how_long = parser.add_mutually_exclusive_group(required=True)
    how_long.add_argument("--follow", metavar="STATE", help="probe until this run state's run ends")
    how_long.add_argument("--minutes", type=float)
    args = parser.parse_args()

    ssm = ssm_client()
    config = service(args.service)
    found = targets(config, ssm)
    if not found:
        print("Nothing to probe: no cloud of quote-service is up.")
        return
    user, token = get_parameter(ssm, "grafana/logs-user"), get_parameter(ssm, "grafana/logs-token")
    loki = (get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push",
            base64.b64encode(f"{user}:{token}".encode()).decode())
    build = None
    if args.follow:
        build = str(json.loads(Path(args.follow).read_text()).get("build", {}).get("number"))
    deadline = time.monotonic() + (args.minutes * 60 if args.minutes else MAX_SECONDS)
    print(f"Probing {len(found)} targets every {INTERVAL}s: " + ", ".join(f"{c} {e}" for c, e, _ in found), flush=True)

    with ThreadPoolExecutor(max_workers=len(found)) as pool:
        while time.monotonic() < deadline and not (args.follow and run_over(args.follow, build)):
            started = time.monotonic()
            results = list(pool.map(probe, found))
            push(loki, results)
            time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))
    print("Prober stopped", flush=True)


if __name__ == "__main__":
    main()
