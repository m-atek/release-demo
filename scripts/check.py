"""Stages 7 and 11, Check: is the new version live and working?

    python scripts/check.py orders-service production --version 1.4.0 --url https://...
    python scripts/check.py orders-service production --version 1.4.0 --local

/health must report the version within the environment's timeout from
services.yaml (30 seconds in staging, 60 in production). Then every path in the
environment's check settings must answer 200. Exits 1 if either fails.
"""

import argparse
import json
import time

from common import http_get, service


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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    parser.add_argument("--version", required=True)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="the function URL of the live alias")
    where.add_argument("--local", action="store_true", help="the local runner, scripts/dev.py")
    args = parser.parse_args()

    env = service(args.service)["environments"][args.environment]
    base_url = (args.url or f"http://127.0.0.1:{env['local_port']}").rstrip("/")
    started = time.monotonic()

    live, seen = wait_for_version(base_url, args.version, env["check"]["timeout_seconds"])
    if not live:
        print(f"FAIL /health reports {seen or 'nothing'}, expected {args.version}")
        raise SystemExit(1)
    print(f"ok   /health reports {args.version} after {time.monotonic() - started:.0f}s")

    failed = False
    for path in env["check"]["paths"]:
        status, _ = http_get(base_url + path)
        failed |= status != 200
        print(f"{'ok  ' if status == 200 else 'FAIL'} {path} answered {status or 'nothing'}")
    if failed:
        print(f"{args.environment.capitalize()} check failed")
        raise SystemExit(1)
    print(f"{args.environment.capitalize()} check passed")


if __name__ == "__main__":
    main()
