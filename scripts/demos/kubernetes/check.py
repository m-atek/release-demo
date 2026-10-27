"""Stages 7 and 11 for Kubernetes, Check: is the new version live and working?

    python scripts/demos/kubernetes/check.py catalog production --version 1.9.1 --url http://127.0.0.1:30082

Through the Service, as a user would reach it: /health must report the
version within the environment's timeout from services.yaml, then each check
path must answer 200. In the cluster, with the build's 10-minute token: every
desired pod ready on that version, and, for the version this run released,
every pod running the image digest from build-info.json, the one the change
names. Prints one line per check and exits 1 if any fails.
"""

import argparse
import json
import sys
import time
from pathlib import Path

# This folder first: its build.py, deploy.py and check.py share names with the Lambda ones in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[2]))
import kube  # noqa: E402
import run_state  # noqa: E402
from common import http_get, service  # noqa: E402


def wait_for_version(base_url, version, timeout):
    deadline = time.monotonic() + timeout
    seen = None
    while True:
        status, body = http_get(f"{base_url}/health")
        if status == 200:
            try:
                seen = json.loads(body).get("version")
            except ValueError:
                seen = None
            if seen == version:
                return True, seen
        if time.monotonic() >= deadline:
            return False, seen
        time.sleep(2)


def cluster_checks(api, namespace, release, desired, version, digest):
    """(ok, line) pairs for the Deployment and its pods."""
    results = []
    deployment = api.deployment(namespace, release) or {"status": {}}
    ready = deployment["status"].get("readyReplicas", 0)
    pods = [kube.pod_summary(p) for p in api.pods(namespace, release)]
    live = [p for p in pods if not p[2]]
    on_version = [p for p in live if p[0] == version and p[1]]
    results.append((ready >= desired and len(on_version) == desired == len(live),
                    f"{len(on_version)} of {desired} pods ready on {version}"
                    + ("" if len(live) == len(on_version) else f", {len(live) - len(on_version)} other")))
    if digest:
        running = {p[3] for p in live}
        results.append((running == {digest}, f"every pod runs {digest[:19]}..., the digest in the change"
                        if running == {digest} else f"pods run {', '.join(sorted(d or '?' for d in running))}, "
                        f"expected {digest}"))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True, help="the Service on the node, from the run state")
    args = parser.parse_args()

    config = service(args.service)
    env = config["environments"][args.environment]
    base_url = args.url.rstrip("/")
    started = time.monotonic()
    state = run_state.load()
    record = state.get("deploys", {}).get(args.environment, {})
    if state.get("version") == args.version and record.get("helm_rolled_back_to"):
        # The deploy saw Helm roll back: no need to wait for a version that won't come.
        print(f"FAIL {args.version} isn't live: {record['helm_rollback_reason']} Helm rolled back to "
              f"{record['helm_rolled_back_to']} (revision {record['helm_rollback_number']})")
        print(f"{args.environment.capitalize()} check failed")
        raise SystemExit(1)

    live, seen = wait_for_version(base_url, args.version, env["check"]["timeout_seconds"])
    if not live:
        print(f"FAIL /health reports {seen or 'nothing'}, expected {args.version}")
        raise SystemExit(1)
    print(f"ok   /health reports {args.version} after {time.monotonic() - started:.0f}s")

    results = []
    for path in env["check"]["paths"]:
        status, _ = http_get(base_url + path)
        results.append((status == 200, f"{path} answered {status or 'nothing'}"))

    digest = None
    if state.get("version") == args.version:
        digest = state.get("deploys", {}).get(args.environment, {}).get("image_digest")
    with kube.credentials(quiet=True) as kubeconfig:
        results += cluster_checks(kube.Api(kubeconfig), env["namespace"], config["kubernetes"]["release"],
                                  env["replicas"], args.version, digest)

    for ok, line in results:
        print(f"{'ok  ' if ok else 'FAIL'} {line}")
    if not all(ok for ok, _ in results):
        print(f"{args.environment.capitalize()} check failed")
        raise SystemExit(1)
    print(f"{args.environment.capitalize()} check passed")


if __name__ == "__main__":
    main()
