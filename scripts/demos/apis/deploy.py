"""Deploy pricing-api: staging all at once, production as a canary.

    python scripts/demos/apis/deploy.py pricing-api staging
    python scripts/demos/apis/deploy.py pricing-api production
    python scripts/demos/apis/deploy.py pricing-api production --back       back to where this run found it
    python scripts/demos/apis/deploy.py pricing-api production --previous   back to the previous release (/rollback)

Downloads the exact package from JFrog Container Registry, checks its
SHA-256 against the run state, updates the function and publishes a Lambda
version whose description is the app version, as the shared deploy does.

Staging then moves the alias live to the new version. Production doesn't:
the new version waits next to live with no traffic, and the production check
runs the canary (canary.py), which gives it its share and decides. --back and
--previous take every weight off the alias and delete any canary alarm left.
"""

import argparse
import hashlib
import sys

import pricing
import run_state
from common import fail, get_parameter, ssm_client


def remove_leftover_alarms():
    names = pricing.canary_alarms()
    if names:
        pricing.cloudwatch_client().delete_alarms(AlarmNames=names)
        print(f"Deleted canary alarms: {', '.join(names)}")


def go_back(client, function, deploys, environment, previous):
    if previous:
        number, app = pricing.shared("deploy").previous_release(client, function)
        current, _ = pricing.routing(client, function)
        deploys[environment] = {"previous_number": current,
                                "previous_version": pricing.app_version(client, function, current)}
    else:
        number = deploys[environment]["previous_number"]
        app = deploys[environment]["previous_version"]
    pricing.route(client, function, number)
    remove_leftover_alarms()
    deploys[environment].update({"rolled_back_to": app, "rolled_back_number": number,
                                 "url": pricing.api_url(environment)})
    deploys[environment].get("canary", {})["state"] = "rolled-back"
    print(f"{function}: live is back on version {number} ({app}), with no other version on it")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    back = parser.add_mutually_exclusive_group()
    back.add_argument("--back", action="store_true", help="move live back to where this run found it")
    back.add_argument("--previous", action="store_true", help="move live to the previous release")
    args = parser.parse_args()

    function = pricing.environment(args.environment)["function"]
    client = pricing.lambda_client()
    state = run_state.load()
    deploys = state.setdefault("deploys", {})

    if args.back or args.previous:
        go_back(client, function, deploys, args.environment, args.previous)
        run_state.save(state)
        return

    token = get_parameter(ssm_client(), "jcr/token")
    package = pricing.shared("publish").download(token, state["package"]["path"])
    actual = hashlib.sha256(package).hexdigest()
    if actual != state["package"]["sha256"]:
        fail(f"{state['package']['path']} has SHA-256 {actual}, but the build recorded {state['package']['sha256']}. "
             f"Not deploying a package that changed after it was published.")
    print(f"SHA-256 verified: {actual}")

    live, weights = pricing.routing(client, function)
    live_app = pricing.app_version(client, function, live)
    client.update_function_code(FunctionName=function, ZipFile=package)
    client.get_waiter("function_updated_v2").wait(FunctionName=function)
    number = client.publish_version(FunctionName=function, Description=state["version"])["Version"]
    record = {"number": number, "previous_number": live, "previous_version": live_app,
              "url": pricing.api_url(args.environment), "deployed_at": run_state.now()}

    if args.environment == "production":
        # A canary left over from an interrupted run loses its share first.
        pricing.route(client, function, live)
        record["canary"] = {"state": "ready"}
        note = f", and took the share of {', '.join(weights)} back" if weights else ""
        print(f"{function}: version {number} ({state['version']}) is published next to live {live} ({live_app}) "
              f"with no traffic yet{note}. The production check runs its canary.")
    else:
        pricing.route(client, function, number)
        print(f"{function}: version {number} ({state['version']}) is live, was {live} ({live_app})")
    deploys[args.environment] = record
    run_state.save(state)


if __name__ == "__main__":
    sys.exit(main())
