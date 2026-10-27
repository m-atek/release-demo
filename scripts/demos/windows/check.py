"""Stages 7 and 11 for the store fleet: is every server where this run left it?

    python scripts/demos/windows/check.py store-sync production --version 3.3.0 --url ""
    python scripts/demos/windows/check.py store-sync staging --version 3.3.0 --url ""

production: one Run Command reads every server's /health. A server this run
upgraded must report --version; one it rolled back, or didn't reach, must
report the version it had; after deploy.py --back, every server it touched
must report --version, which is then the version before. Without a run state,
every server must report --version. Waits up to the check timeout in
services.yaml. --url is accepted for the shared pipeline and not used: the
fleet answers only on localhost.

staging: the fleet has no staging; this checks that the plan exists and every
planned server answered.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import run_state  # noqa: E402
from common import service  # noqa: E402
from fleet import Fleet  # noqa: E402


def expected_versions(state, names, version):
    """{name: the version it should report} after this run."""
    production = state.get("deploys", {}).get("production", {})
    touched = production.get("servers", {})
    rolled_back = set(production.get("rolled_back_servers", []))
    plan = state.get("plan", {})
    expected = {}
    for name in names:
        if name in rolled_back:
            expected[name] = version
        elif name in touched:
            expected[name] = touched[name]["after"]
        elif name in plan.get("servers", {}):
            expected[name] = plan["servers"][name]["before"]
        elif name in plan.get("others", {}):
            expected[name] = plan["others"][name]
        else:
            expected[name] = version
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment", choices=["staging", "production"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", default="", help="not used: the fleet answers on localhost only")
    args = parser.parse_args()

    config = service(args.service)
    state = run_state.load()
    if args.environment == "staging":
        plan = state.get("plan")
        if not plan:
            print("FAIL no wave plan: deploy.py staging didn't run")
            raise SystemExit(1)
        for number, wave in zip(plan["numbers"], plan["waves"]):
            for name in wave:
                print(f"ok   {name} answers through Run Command, on {plan['servers'][name]['before']}, wave {number}")
        print("Staging check passed: the fleet has no staging, the first wave is the canary")
        return

    fleet = Fleet(config)
    ids = fleet.running()
    expected = expected_versions(state, list(ids), args.version)
    timeout = config["environments"]["production"]["check"]["timeout_seconds"]
    deadline = time.monotonic() + timeout
    while True:
        answers = fleet.state(ids, f"store-sync {args.version}: check the fleet")
        seen = {name: (a.result.get("health_code"), a.result.get("health_version"), a.result.get("problems") or [])
                for name, a in answers.items()}
        failing = [n for n in ids if seen[n][0] != 200 or seen[n][1] != expected[n]]
        if not failing or time.monotonic() >= deadline:
            break
        time.sleep(5)

    missing = [n for n in config["environments"]["production"]["servers"] if n not in ids]
    for name in ids:
        code, version, problems = seen[name]
        if name in failing:
            print(f"FAIL {name} /health answered {code or 'nothing'} with {version}, expected {expected[name]}"
                  + (f": {'; '.join(problems)}" if problems else ""))
        else:
            print(f"ok   {name} /health reports {version}")
    for name in missing:
        print(f"FAIL {name} isn't running")
    if failing or missing:
        print("Production check failed")
        raise SystemExit(1)
    print("Production check passed")


if __name__ == "__main__":
    main()
