"""Check pricing-api: through its API in staging, as a canary in production.

    python scripts/demos/apis/check.py pricing-api staging --version 2.1.1 --url https://...
    python scripts/demos/apis/check.py pricing-api production --version 2.1.1 --url https://...

When the production deploy of this run has published the version it checks,
the production check is the canary (canary.py): a share of live traffic for
the set time, with CloudWatch's alarms deciding. Its time comes from
services.yaml: canary.minutes, or canary.minutes_on_calls with the option
call. A canary that fails exits 1, so the pipeline's rollback runs.

Then, and in staging, the same check as the shared one, through API Gateway:
/health must report the version within the environment's timeout, and every
path in its check settings must answer 200. One line per check: ok or FAIL.
"""

import argparse
import signal
import sys

import pricing
import run_state
from common import http_get


def canary_due(state, environment, version):
    deploy = state.get("deploys", {}).get(environment, {})
    return (environment == "production" and version == state.get("version")
            and deploy.get("canary", {}).get("state") == "ready")


def canary_minutes(state):
    settings = pricing.config()["canary"]
    return settings["minutes_on_calls"] if state.get("option") == "call" else settings["minutes"]


def run_canary(state):
    import canary
    from grafana import connect

    # Jenkins stops a step with SIGTERM: the canary's finally takes the weight back.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    _, annotations = connect()
    return canary.Canary(state, canary_minutes(state), pricing.lambda_client(), pricing.cloudwatch_client(),
                         annotations, out=lambda text: print(text, flush=True)).run()


def check_api(base_url, version, settings):
    live, seen = pricing.shared("check").wait_for_version(base_url, version, settings["timeout_seconds"])
    if not live:
        print(f"FAIL /health reports {seen or 'nothing'}, expected {version}")
        return False
    print(f"ok   /health reports {version}")
    passed = True
    for path in settings["paths"]:
        status, _ = http_get(base_url + path)
        passed &= status == 200
        print(f"{'ok  ' if status == 200 else 'FAIL'} {path} answered {status or 'nothing'}")
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True, help="the API Gateway address")
    args = parser.parse_args()

    state = run_state.load()
    if canary_due(state, args.environment, args.version):
        if not run_canary(state):
            print(f"Production check failed: the canary of {args.version} raised an alarm")
            raise SystemExit(1)

    settings = pricing.environment(args.environment)["check"]
    if not check_api(args.url.rstrip("/"), args.version, settings):
        print(f"{args.environment.capitalize()} check failed")
        raise SystemExit(1)
    print(f"{args.environment.capitalize()} check passed")


if __name__ == "__main__":
    main()
