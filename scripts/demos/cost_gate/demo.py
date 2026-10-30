"""scripts/demo.sh up, down, status and check for video 18, the cost check.

    python scripts/demos/cost_gate/demo.py up       apply what is missing (plans shown, Terraform asks first)
    python scripts/demos/cost_gate/demo.py down     the switch off; the tables stay (cents of storage)
    python scripts/demos/cost_gate/demo.py status   the switch, the tables, the policies, the timeout, Fiona's group
    python scripts/demos/cost_gate/demo.py check    the same as ok or FAIL lines, plus the prices' date and the panel

None of them calls Claude. up applies infra/demos/18-cost-gate (the two
tables and each function's policy) and, if a function still has its old
10-second timeout, infra/ (the timeout of 30 seconds 1.10.0 needs). Both
are IAM or Lambda changes: you run up, and each apply shows its plan first.
"""

import argparse
import datetime
import json
import subprocess
import sys

import costs
from common import ROOT, aws_session
from llm import prices

ENVIRONMENTS = ("staging", "production")
TIMEOUT = 30
POLICY = "cost-gate-messages-and-key"


def function(name):
    return costs.service(costs.SERVICE)["environments"][name]["function"]


def checks():
    """[(ok, what)] for everything the demo needs, read only."""
    session = aws_session()
    dynamodb, iam, lam = session.client("dynamodb"), session.client("iam"), session.client("lambda")
    found = []
    for name in ENVIRONMENTS:
        table = f"orders-service-messages-{name}"
        try:
            ttl = dynamodb.describe_time_to_live(TableName=table)["TimeToLiveDescription"]
            found.append((ttl.get("TimeToLiveStatus") == "ENABLED", f"table {table}, TTL on ttl"))
        except dynamodb.exceptions.ResourceNotFoundException:
            found.append((False, f"table {table}: not there (demo.sh up cost-gate)"))
        role = f"{function(name)}-function"
        try:
            iam.get_role_policy(RoleName=role, PolicyName=POLICY)
            found.append((True, f"policy {POLICY} on {role}"))
        except iam.exceptions.NoSuchEntityException:
            found.append((False, f"policy {POLICY} on {role}: not there (demo.sh up cost-gate)"))
        timeout = lam.get_function_configuration(FunctionName=function(name))["Timeout"]
        found.append((timeout >= TIMEOUT, f"{function(name)} timeout {timeout} s (1.10.0 needs {TIMEOUT})"))
    switch = load_switch().value()
    found.append((switch in ("on", "off"), f"switch cost-check/enabled: {switch or 'not readable'}"))
    read = datetime.date.fromisoformat(str(prices()["read"]))
    age = (datetime.date.today() - read).days
    found.append((age <= 7, f"prices.yaml read {read:%d %B %Y}, {age} days ago (re-read the pages on recording day)"))
    found.append(panel_check())
    found.append(servicenow_check())
    return found


def load_switch():
    import importlib.util

    spec = importlib.util.spec_from_file_location("cost_switch", costs.HERE / "switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SWITCH


def panel_check():
    title = "Monthly cost at expected volume, by version (USD)"
    dashboard = json.loads((ROOT / "grafana" / "releases.json").read_text())
    if not any(panel.get("title") == title for panel in dashboard["panels"]):
        return False, "grafana/releases.json has no cost panel"
    try:
        from common import get_parameter, ssm_client
        from grafana_setup import Grafana

        ssm = ssm_client()
        grafana = Grafana(get_parameter(ssm, "grafana/stack-url"), get_parameter(ssm, "grafana/service-account-token"))
        status, body = grafana.call("GET", "/api/dashboards/uid/release-demo")
        saved = status == 200 and any(p.get("title") == title for p in body["dashboard"]["panels"])
        return saved, f"Grafana's Releases dashboard {'has' if saved else 'lacks'} the cost panel" + (
            "" if saved else " (scripts/grafana_setup.py)")
    except BaseException as error:
        return False, f"Grafana not read ({error.__class__.__name__})"


def servicenow_check():
    try:
        from common import get_parameter, ssm_client
        from servicenow_setup import COST_FIELD, SETUP_USER, ServiceNow

        ssm = ssm_client()
        sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER, get_parameter(ssm, "servicenow/admin-password"))
        group = sn.find("sys_user_group", "name=Cost approvers", "sys_id")
        members = sn.call("GET", "/api/now/table/sys_user_grmember", sysparm_query=f"group={group['sys_id']}",
                          sysparm_fields="user", sysparm_display_value="true") if group else []
        names = sorted(member["user"]["display_value"] for member in members)
        field = sn.find("sys_dictionary", f"name=change_request^element={COST_FIELD}", "sys_id")
        ok = names == ["Fiona Finance"] and field is not None
        return ok, f"ServiceNow: Cost approvers {names or 'missing'}, field Cost review {'there' if field else 'missing'}"
    except BaseException as error:
        return False, f"ServiceNow not read ({error.__class__.__name__}): wake it, or run setup.py"


def up():
    found = checks()
    if not all(ok for ok, what in found if what.startswith(("table ", "policy "))):
        print("Applying infra/demos/18-cost-gate: the tables and each function's policy. Terraform shows the plan first.")
        subprocess.run([str(costs.HERE / "tf.sh"), "apply"], check=True)
    if not all(ok for ok, what in found if " timeout " in what):
        print("Applying infra/: the functions' timeout of 30 seconds. Check the plan: only the timeout changes.")
        subprocess.run([str(ROOT / "scripts" / "tf.sh"), "apply"], check=True)
    return show()


def show(strict=False):
    found = checks()
    for ok, what in found:
        print(f"{'ok  ' if ok else 'FAIL'} {what}" if strict else f"  {what}")
    return 0 if all(ok for ok, _ in found) or not strict else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status", "check"])
    args = parser.parse_args()
    if args.action == "up":
        return up()
    if args.action == "down":
        load_switch().set("off")
        print("The tables stay: on demand they cost only their storage, cents a month.")
        return 0
    return show(strict=args.action == "check")


if __name__ == "__main__":
    sys.exit(main())
