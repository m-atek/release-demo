"""Set up the ServiceNow PDI for the release demo. Idempotent.

    python scripts/servicenow_setup.py              set up, or check, everything
    python scripts/servicenow_setup.py --test       also prove the approval rule with a test change

Rebuilds a fresh PDI in about 10 minutes. It signs in as the setup user
release-demo.setup (admin and snc_basic_auth_api_access roles; password in SSM
as servicenow/admin-password), because new instances refuse password sign-in
on the REST API for users without snc_basic_auth_api_access. Then it creates:

- the users Sam Developer and Alex Change Manager, with the passwords in SSM,
  and release.pipeline: web service access only, roles itil and
  snc_basic_auth_api_access, and a generated password it stores in SSM. All
  three in Europe/Amsterdam time, as Slack and the terminal show it;
- the read-only users of video 11: ines.auditor, who signs in to the UI, and
  evidence.reader, web service access only, with a generated password in
  SSM; both snc_read_only, in no group and no approver;
- the groups Platform team (Sam, release.pipeline) and Change approvers (Alex);
- a CMDB item per service in services.yaml, and jfrog-container-registry;
- normal change approval by Change approvers only, once, at Assess. Out of the
  box the Normal Change Policy asks the assignment group at Assess, which
  would let Sam approve his own change, and a manager or the CAB at Authorize;
- the standard change templates from services.yaml, pre-approved.

Every call detects the hibernation page: wake the instance at
developer.servicenow.com.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from common import fail, get_parameter, put_parameter, services, ssm_client

SETUP_USER = "release-demo.setup"
ASSESS = "-4"
CANCELED = "4"

PEOPLE = {
    "sam.developer": {"first_name": "Sam", "last_name": "Developer", "password": "servicenow/sam-password",
                      "roles": ["itil"], "time_zone": "Europe/Amsterdam"},
    "alex.change.manager": {"first_name": "Alex", "last_name": "Change Manager", "password": "servicenow/alex-password",
                            "roles": ["itil", "approver_user"], "time_zone": "Europe/Amsterdam"},
    "release.pipeline": {"first_name": "Release", "last_name": "Pipeline", "password": "servicenow/release-pipeline-password",
                         "roles": ["itil", "snc_basic_auth_api_access"], "machine": True, "time_zone": "Europe/Amsterdam",
                         "generated": True},
    # Video 11: the auditor reads. snc_read_only refuses every write, whatever her other roles allow.
    # Neither is in a group or an approver.
    "ines.auditor": {"first_name": "Ines", "last_name": "Auditor", "password": "servicenow/ines-password",
                     "roles": ["itil", "snc_read_only"], "time_zone": "Europe/Amsterdam"},
    "evidence.reader": {"first_name": "Evidence", "last_name": "Reader", "password": "servicenow/evidence-reader-password",
                        "roles": ["itil", "snc_read_only", "snc_basic_auth_api_access"], "machine": True,
                        "generated": True},
}
GROUPS = {
    "Platform team": {"members": ["sam.developer", "release.pipeline"],
                      "description": "Builds and runs the order services. Asks for releases; can't approve them."},
    "Change approvers": {"members": ["alex.change.manager"],
                         "description": "Approves normal changes. Nobody in it asks for releases."},
}
APPROVAL_DEFINITION = "Change approvers Approval"
# CMDB items for the platform itself, beside one per service: video 10's
# incident names the registry whose token leaked.
PLATFORM_ITEMS = {
    "jfrog-container-registry": "JFrog Container Registry on the tools host: the release pipeline's packages.",
}


class ServiceNow:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method, path, body=None, **query):
        query = {k: v for k, v in query.items() if v is not None}
        request = urllib.request.Request(
            f"{self.url}{path}{'?' + urllib.parse.urlencode(query) if query else ''}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            self.asleep(raw)
            fail(f"ServiceNow {method} {path}: {error.code} {raw[:300]}")
        self.asleep(raw)
        return json.loads(raw)["result"] if raw else None

    @staticmethod
    def asleep(raw):
        if "hibernat" in raw.lower() or "developer.servicenow.com" in raw.lower() and "<html" in raw.lower():
            fail("ServiceNow is asleep: wake it at developer.servicenow.com")

    def find(self, table, query, fields=None):
        rows = self.call("GET", f"/api/now/table/{table}", sysparm_query=query, sysparm_limit=1, sysparm_fields=fields)
        return rows[0] if rows else None

    def ensure(self, table, query, values, label):
        """Create the record if the query finds none, else bring its values in line."""
        current = self.find(table, query)
        if current is None:
            created = self.call("POST", f"/api/now/table/{table}", values)
            print(f"  {label}: created")
            return created
        changed = {k: v for k, v in values.items() if str(raw(current.get(k, ""))) != str(v)}
        if changed:
            current = self.call("PATCH", f"/api/now/table/{table}/{current['sys_id']}", changed)
            print(f"  {label}: updated {', '.join(changed)}")
        else:
            print(f"  {label}: already there")
        return current


def raw(value):
    """A reference field comes back as {link, value}: compare its sys_id."""
    return value.get("value") if isinstance(value, dict) else value


def generated_password():
    """Meets the PDI's password policy, which needs a special character.
    A password that misses the policy is dropped without an error."""
    core = base64.urlsafe_b64encode(os.urandom(18)).decode().replace("-", "x").replace("_", "y")
    return core[:12] + "#" + core[12:] + "!Aa1"


def setup_people(sn, ssm):
    users = {}
    for user_name, person in PEOPLE.items():
        password = get_parameter_or_none(ssm, person["password"])
        if password is None and person.get("generated"):
            password = generated_password()
            put_parameter(ssm, person["password"], password)
            print(f"  {user_name}: generated a password and stored it in SSM")
        elif password is None:
            fail(f"/release-demo/{person['password']} isn't set yet. See: python scripts/secrets.py set servicenow")
        # Web service access only: no UI. Not an internal integration user: those can't hold a password.
        values = {"user_name": user_name, "first_name": person["first_name"], "last_name": person["last_name"],
                  "email": f"{user_name}@example.invalid", "active": "true", "password_needs_reset": "false",
                  "web_service_access_only": "true" if person.get("machine") else "false",
                  "internal_integration_user": "false"}
        # Video 10: the incident's times read like Slack's and the terminal's.
        if person.get("time_zone"):
            values["time_zone"] = person["time_zone"]
        user = sn.ensure("sys_user", f"user_name={user_name}", values, f"user {user_name}")
        # Passwords need display-value input. Kept in line with SSM on every run, quietly; a password the
        # instance's policy refuses is dropped without an error, so check it took.
        sn.call("PATCH", f"/api/now/table/sys_user/{user['sys_id']}", {"user_password": password},
                sysparm_input_display_value="true")
        if not sn.find("sys_user", f"sys_id={user['sys_id']}", "user_password").get("user_password"):
            fail(f"ServiceNow refused the password for {user_name}: it doesn't meet the instance's password "
                 f"policy (upper and lower case, a digit, a special character). Set another in SSM.")
        users[user_name] = user["sys_id"]
        for role in person["roles"]:
            found = sn.find("sys_user_role", f"name={role}", "sys_id")
            if found is None:
                fail(f"This instance has no role {role}. As admin: System Definition, Plugins, find the plugin "
                     f"that provides {role} (for snc_read_only: Read Only Role) and activate it, then run this again.")
            role_id = found["sys_id"]
            sn.ensure("sys_user_has_role", f"user={user['sys_id']}^role={role_id}",
                      {"user": user["sys_id"], "role": role_id}, f"  role {role}")
    return users


def get_parameter_or_none(ssm, name):
    value = ssm.get_parameter(Name=f"/release-demo/{name}", WithDecryption=True)["Parameter"]["Value"]
    return None if value == "unset" else value


def setup_groups(sn, users):
    groups = {}
    for name, group in GROUPS.items():
        record = sn.ensure("sys_user_group", f"name={name}", {"name": name, "description": group["description"]},
                           f"group {name}")
        groups[name] = record["sys_id"]
        for member in group["members"]:
            sn.ensure("sys_user_grmember", f"group={record['sys_id']}^user={users[member]}",
                      {"group": record["sys_id"], "user": users[member]}, f"  member {member}")
    return groups


def setup_cmdb(sn, groups):
    """The CMDB item each change names, plus one per environment where services.yaml gives one."""
    items = {}
    for name, config in services().items():
        item = config["servicenow"]["cmdb_item"]
        extra = [env["cmdb_item"] for env in config.get("environments", {}).values() if env.get("cmdb_item")]
        for each in dict.fromkeys([item, *extra]):
            record = sn.ensure("cmdb_ci_appl", f"name={each}", {
                "name": each, "short_description": f"{name}: released by the release demo pipeline. Demo system.",
                "support_group": groups["Platform team"], "operational_status": "1"}, f"CMDB item {each}")
            if each == item:
                items[name] = record["sys_id"]
    for name, text in PLATFORM_ITEMS.items():
        sn.ensure("cmdb_ci_appl", f"name={name}", {"name": name, "short_description": text,
                                                   "support_group": groups["Platform team"], "operational_status": "1"},
                  f"CMDB item {name}")
    return items


def setup_approvals(sn, groups):
    """One approval, by Change approvers, at Assess. Nothing at Authorize."""
    policy = sn.find("chg_policy", "name=Normal Change Policy", "sys_id")
    if not policy:
        fail("No Normal Change Policy on this instance: the approval steps need the UI. See RUNBOOK.md.")
    template = sn.find("chg_approval_def", "name=CAB Approval")
    definition = sn.ensure("chg_approval_def", f"name={APPROVAL_DEFINITION}", {
        "name": APPROVAL_DEFINITION, "table": "change_request", "approver_source": template["approver_source"],
        "approval_action": template["approval_action"], "group": groups["Change approvers"],
        "mandatory": "true", "wait_for": template["wait_for"], "advanced": "false"}, f"approval definition {APPROVAL_DEFINITION}")

    decisions = sn.call("GET", "/api/now/table/sys_decision_question", sysparm_query=f"decision_table={policy['sys_id']}",
                        sysparm_fields="sys_id,label,condition,answer,active")
    for decision in decisions:
        at_assess = "change_request.state=-4" in decision["condition"]
        wanted = ({"answer": definition["sys_id"], "active": "true"} if at_assess else {"active": "false"})
        changed = {k: v for k, v in wanted.items() if str(raw(decision.get(k, ""))) != v}
        if changed:
            sn.call("PATCH", f"/api/now/table/sys_decision_question/{decision['sys_id']}", changed)
        what = "asks Change approvers" if at_assess else "off"
        print(f"  decision '{decision['label']}': {what}{' (changed)' if changed else ''}")
    return definition


def template_texts(service, kind):
    """The fields a standard change template carries. The instance requires these.
    A service may give its own per template in services.yaml
    (servicenow.template_texts.<kind>), which replace these field by field."""
    own = services()[service]["servicenow"].get("template_texts", {}).get(kind, {})
    if kind == "standard":
        texts = {
            "short_description": f"Routine release of {service}",
            "description": f"A pre-approved release of {service}, started from Slack and run by the release pipeline: "
                           "tests, quality gate, SBOM and scan, staging and its check, then production and its check.",
            "implementation_plan": "The pipeline deploys the package stored in JFrog Container Registry, verified by its "
                                   "SHA-256, to staging and checks it, then deploys the same package to production and "
                                   "moves the live alias. Each stage adds a work note to this change.",
            "backout_plan": "Automatic: if the production check fails, the pipeline moves the live alias back to the "
                            "previous version, checks it, and closes this change as unsuccessful.",
            "test_plan": "Unit tests, the SonarQube quality gate, an SBOM scan with no critical findings, the staging "
                         "health check, and the production health and smoke check.",
            "justification": f"Routine releases of {service} follow the same automated, tested path every time.",
            "risk_impact_analysis": "Low: the same package passed staging, and the rollback is automatic.",
        }
    else:
        texts = {
            "short_description": f"Rollback of {service}",
            "description": f"Moves {service} in production back to the previous version, recorded as a pre-approved "
                           "change. Started with /rollback in Slack, or by the pipeline after a failed production check.",
            "implementation_plan": "Move the production live alias to the previous published version and check /health.",
            "backout_plan": "Move the alias forward again to the version that was rolled back.",
            "test_plan": "/health must report the previous version within 60 seconds.",
            "justification": "Restores the service quickly after a failed release.",
            "risk_impact_analysis": "Low: production returns to the version that ran before.",
        }
    return {**texts, **own}


def encoded(values):
    """A ServiceNow template value: field=value pairs joined by ^, with ^ itself doubled."""
    return "^".join(f"{field}={str(value).replace('^', '^^')}" for field, value in values.items()) + "^EQ"


def setup_templates(sn, groups, items):
    """Pre-approved standard change templates, through a proposal ServiceNow approves into a template."""
    category = sn.find("sc_category", "title=Standard Changes", "sys_id")
    for service, config in services().items():
        for kind, name in config["servicenow"]["templates"].items():
            if sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id"):
                print(f"  template '{name}': already there")
                continue
            values = {**template_texts(service, kind), "assignment_group": groups["Platform team"],
                      "cmdb_ci": items[service]}
            own = config["servicenow"].get("template_texts", {}).get(kind, {})
            if kind == "standard" and config["servicenow"].get("backout_plan") and "backout_plan" not in own:
                values["backout_plan"] = config["servicenow"]["backout_plan"]
            proposal = sn.find("std_change_proposal", f"template_name={name}^state!=4", "sys_id,approval")
            if proposal is None:
                proposal = sn.call("POST", "/api/now/table/std_change_proposal", {
                    "proposal_type": "1", "template_name": name, "short_description": f"New standard change: {name}",
                    "category": category["sys_id"] if category else "", "template_value": encoded(values),
                    "business_justification": values["justification"]})
            if raw(proposal.get("approval")) != "approved":
                sn.call("PATCH", f"/api/now/table/std_change_proposal/{proposal['sys_id']}", {"approval": "approved"})
            for _ in range(10):
                if sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id"):
                    break
                time.sleep(3)
            else:
                fail(f"ServiceNow didn't build the template '{name}' from its approved proposal. See RUNBOOK.md.")
            print(f"  template '{name}': created, pre-approved")


def prove_access(sn, ssm):
    """release.pipeline signs in to the Change Management API the way the pipeline will."""
    pipeline = ServiceNow(sn.url, "release.pipeline", get_parameter(ssm, "servicenow/release-pipeline-password"))
    pipeline.call("GET", "/api/sn_chg_rest/change/model", sysparm_limit=1)
    print("  release.pipeline: signs in to the Change Management API")


def prove_standard(sn, ssm):
    """A standard change from the routine template comes out pre-approved, with no approver."""
    pipeline = ServiceNow(sn.url, "release.pipeline", get_parameter(ssm, "servicenow/release-pipeline-password"))
    name = next(iter(services().values()))["servicenow"]["templates"]["standard"]
    producer = sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id")
    change = pipeline.call("POST", f"/api/sn_chg_rest/change/standard/{producer['sys_id']}",
                           {"short_description": "Standard change check by servicenow_setup.py: cancelled right after"})
    change_id, number = change["sys_id"]["value"], change["number"]["value"]
    record = sn.call("GET", f"/api/now/table/change_request/{change_id}", sysparm_fields="approval,state,chg_model",
                     sysparm_display_value="true")
    approvers = sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change_id}",
                        sysparm_fields="sys_id")
    sn.call("PATCH", f"/api/sn_chg_rest/change/standard/{change_id}", {"state": CANCELED,
            "close_notes": "Standard change check by servicenow_setup.py."})
    print(f"  test change {number}: {record['chg_model']['display_value']}, approval {record['approval']}, "
          f"state {record['state']}, {len(approvers)} approvers; cancelled")
    if record["approval"] != "Approved" or approvers:
        fail("A standard change should come out approved with no approver. See RUNBOOK.md.")


def prove_approvals(sn, users, groups, items):
    """A normal change, moved to Assess, must ask Alex and nobody else."""
    change = sn.call("POST", "/api/sn_chg_rest/change/normal", {
        "short_description": "Approval check by servicenow_setup.py: cancelled right after",
        "assignment_group": groups["Platform team"], "requested_by": users["sam.developer"],
        "cmdb_ci": next(iter(items.values()))})
    change_id = change["sys_id"]["value"]
    number = change["number"]["value"]
    sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": ASSESS})
    approvals = sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change_id}",
                        sysparm_fields="approver,state", sysparm_display_value="true")
    approvers = sorted({a["approver"]["display_value"] for a in approvals})
    state = "not checked"
    if approvers == ["Alex Change Manager"]:
        # As if Alex approved: the change should pass Authorize with no second approval.
        for approval in sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change_id}",
                                sysparm_fields="sys_id"):
            sn.call("PATCH", f"/api/now/table/sysapproval_approver/{approval['sys_id']}", {"state": "approved"})
        for _ in range(10):
            time.sleep(3)
            state = sn.call("GET", f"/api/now/table/change_request/{change_id}", sysparm_fields="state",
                            sysparm_display_value="true")["state"]
            if state == "Scheduled":
                break
    sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": CANCELED,
            "close_notes": "Approval check by servicenow_setup.py."})
    print(f"  test change {number}: approvers at Assess {approvers or 'none'}; after approval: {state}; cancelled")
    if approvers != ["Alex Change Manager"]:
        fail("The approval rule isn't right: only Alex Change Manager should be asked. See RUNBOOK.md for the UI steps.")
    if state != "Scheduled":
        fail(f"After Alex's approval the change should be Scheduled, it is {state}: a second approval step is left.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test", action="store_true", help="prove the approval rule with a test change")
    args = parser.parse_args()

    ssm = ssm_client()
    sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER, get_parameter(ssm, "servicenow/admin-password"))
    sn.find("sys_user", f"user_name={SETUP_USER}", "sys_id")
    print("People")
    users = setup_people(sn, ssm)
    print("Groups")
    groups = setup_groups(sn, users)
    print("CMDB")
    items = setup_cmdb(sn, groups)
    print("Normal change approval")
    setup_approvals(sn, groups)
    print("Standard change templates")
    setup_templates(sn, groups, items)
    print("Pipeline access")
    prove_access(sn, ssm)
    if args.test:
        print("Approval check")
        prove_approvals(sn, users, groups, items)
        prove_standard(sn, ssm)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
