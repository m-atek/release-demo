"""scripts/demo.sh up, down and status for demo 4, pricing-api.

    python scripts/demos/apis/demo.py up       create pricing-api if it isn't there, wait until both APIs answer
    python scripts/demos/apis/demo.py down     nothing to stop; clears what an interrupted canary left
    python scripts/demos/apis/demo.py status   what live runs in each environment, any canary weight or alarm

pricing-api has no server: Lambda and API Gateway charge per request, so it
costs nothing while nobody calls it, and down has nothing to stop. Terraform
creates it once (scripts/demos/apis/tf.sh apply, which up runs if needed).
"""

import argparse
import json
import subprocess
import sys
import time

import pricing
from common import aws_session, http_get

ENVIRONMENTS = ("staging", "production")


def exists():
    client = aws_session().client("lambda")
    try:
        client.get_function(FunctionName=pricing.environment("production")["function"])
        return True
    except client.exceptions.ResourceNotFoundException:
        return False


def health(name):
    status, body = http_get(f"{pricing.api_url(name)}/health", timeout=10)
    return json.loads(body).get("version") if status == 200 else None


def describe(client, name):
    function = pricing.environment(name)["function"]
    main, weights = pricing.routing(client, function)
    text = f"live on version {main} ({pricing.app_version(client, function, main)})"
    for number, weight in weights.items():
        text += f", {weight:.0%} to version {number} ({pricing.app_version(client, function, number)})"
    return text, weights


def up():
    if not exists():
        print("pricing-api isn't there yet: applying infra/demos/4-apis. Terraform shows its plan and asks first.")
        subprocess.run([str(pricing.HERE / "tf.sh"), "apply"], check=True)
    deadline = time.monotonic() + 60
    versions = {}
    while time.monotonic() < deadline:
        versions = {name: health(name) for name in ENVIRONMENTS}
        if all(versions.values()):
            print("Ready: " + ", ".join(f"{name} {version}" for name, version in versions.items()))
            return
        time.sleep(3)
    print("Not answering after 60s: " + ", ".join(f"{n} {v or 'nothing'}" for n, v in versions.items()))
    raise SystemExit(1)


def down():
    client = pricing.lambda_client()
    function = pricing.environment("production")["function"]
    main, weights = pricing.routing(client, function)
    alarms = pricing.canary_alarms()
    if weights and alarms:
        print(f"A canary is running ({', '.join(alarms)}): left as it is. Run down again when it has finished.")
    elif weights:
        pricing.route(client, function, main)
        print(f"An interrupted canary still had traffic: version {main} takes it all again.")
    elif alarms:
        pricing.cloudwatch_client().delete_alarms(AlarmNames=alarms)
        print(f"Deleted leftover canary alarms: {', '.join(alarms)}")
    print("pricing-api has no server to stop: Lambda and API Gateway cost nothing while nobody calls them.")


def status():
    if not exists():
        print("pricing-api: not created (scripts/demo.sh up apis creates it)")
        return
    client = pricing.lambda_client()
    for name in ENVIRONMENTS:
        text, _ = describe(client, name)
        print(f"  {name:<11} {text}; API /health: {health(name) or 'no answer'}")
    alarms = pricing.canary_alarms()
    print(f"  canary alarms: {', '.join(alarms) if alarms else 'none'}")
    print("  cost: per request only, nothing while idle")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    args = parser.parse_args()
    {"up": up, "down": down, "status": status}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
