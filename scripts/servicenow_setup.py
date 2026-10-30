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
- the groups Platform team (Sam, release.pipeline, and Pieter On Call, demo
  19's on-call engineer, who has no password and never signs in) and Change
  approvers (Alex);
- a CMDB item per service in services.yaml, and jfrog-container-registry;
- normal change approval by Change approvers only, once, at Assess. Out of the
  box the Normal Change Policy asks the assignment group at Assess, which
  would let Sam approve his own change, and a manager or the CAB at Authorize;
- the standard change templates from services.yaml, pre-approved;
- for videos 12 and 13, the user ai.notes: web service access only, roles
  itil and snc_basic_auth_api_access, a generated password in SSM, in no
  group. It writes work notes and attaches evidence.json, and approves nothing;
- for video 18, Fiona Finance (fiona.finance, itil and approver_user, her
  password from SSM, which you set, in Europe/Amsterdam time) as the only
  member of Cost approvers, the change field Cost review (u_cost_review) next
  to Type, and the Normal Change Policy's decisions at Assess: empty asks
  Change approvers, as before; "Cost approvers" asks Cost approvers only;
  "Cost and change approvers" asks both, and both must approve. Without
  Fiona's password this part waits for it;
- video 16's two fields on the change, Code written by and Code reviewed by,
  on its form next to Requested by;
- the business rule that tells the approval bridge when a change's approval
  is decided, once the bridge is applied (scripts/approval_bridge.py);
- for demo 19, the Emergency Change Policy's one approval, by Change
  approvers at Authorize, and the template Reset of catalog production.

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

import approval_bridge
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
    # Videos 12 and 13: writes the AI notes on changes. In no group, never an approver.
    "ai.notes": {"first_name": "AI", "last_name": "Notes", "password": "servicenow/ai-notes-password",
                 "roles": ["itil", "snc_basic_auth_api_access"], "machine": True, "generated": True},
    # Video 18: the cost approver. Set up once her password is in SSM. Her approvals show Amsterdam time, as Alex's do.
    "fiona.finance": {"first_name": "Fiona", "last_name": "Finance", "password": "servicenow/fiona-password",
                      "roles": ["itil", "approver_user"], "optional": "video 18", "time_zone": "Europe/Amsterdam"},
    # Video 11: the auditor reads. snc_read_only refuses every write, whatever her other roles allow.
    # Neither is in a group or an approver.
    "ines.auditor": {"first_name": "Ines", "last_name": "Auditor", "password": "servicenow/ines-password",
                     "roles": ["itil", "snc_read_only"], "time_zone": "Europe/Amsterdam"},
    "evidence.reader": {"first_name": "Evidence", "last_name": "Reader", "password": "servicenow/evidence-reader-password",
                        "roles": ["itil", "snc_read_only", "snc_basic_auth_api_access"], "machine": True,
                        "generated": True},
    # Demo 19: the on-call engineer the drift incidents and emergency changes name. He never
    # signs in, so he has no password; he is in Platform team, not in Change approvers.
    "pieter.oncall": {"first_name": "Pieter", "last_name": "On Call", "password": None, "roles": ["itil"],
                      "time_zone": "Europe/Amsterdam"},
}
GROUPS = {
    "Platform team": {"members": ["sam.developer", "release.pipeline", "pieter.oncall"],
                      "description": "Builds and runs the order services. Asks for releases; can't approve them."},
    "Change approvers": {"members": ["alex.change.manager"],
                         "description": "Approves normal changes. Nobody in it asks for releases."},
    "Cost approvers": {"members": ["fiona.finance"], "optional": "video 18",
                       "description": "Approves releases whose cost check is above the limit. Nobody in it asks for releases."},
}
APPROVAL_DEFINITION = "Change approvers Approval"
# Video 18: the change field the cost check sets, and the decision it adds at Assess.
COST_FIELD = "u_cost_review"
COST_CHOICES = [("cost_approvers", "Cost approvers"), ("cost_and_change_approvers", "Cost and change approvers")]
COST_DEFINITION = "Cost approvers Approval"
COST_DECISION = "Assess: Cost approvers"
# Change approvers are asked at Assess when Cost review is empty or asks both groups.
CHANGE_APPROVERS_CLAUSE = f"change_request.{COST_FIELD}ISEMPTY^ORchange_request.{COST_FIELD}=cost_and_change_approvers"
COST_CONDITION = f"change_request.state=-4^change_request.{COST_FIELD}ISNOTEMPTY^EQ"
COST_FIELD_STEPS = """\
  Make the field by hand, once: open any change, right-click the form header,
  Configure, Form Layout, Create new field: name Cost review, type Choice,
  choices cost_approvers (Cost approvers) and cost_and_change_approvers (Cost
  and change approvers), with -- None --. Put it right under Type, Save, and
  run this script again."""
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
        password = get_parameter_or_none(ssm, person["password"]) if person["password"] else None
        if password is None and person["password"] and (person.get("machine") or person.get("generated")):
            password = generated_password()
            put_parameter(ssm, person["password"], password)
            print(f"  {user_name}: generated a password and stored it in SSM")
        elif password is None and person["password"] and person.get("optional"):
            print(f"  {user_name}: skipped for now ({person['optional']}): set /release-demo/{person['password']} "
                  f"with scripts/secrets.py, then run this again")
            continue
        elif password is None and person["password"]:
            fail(f"/release-demo/{person['password']} isn't set yet. See: python scripts/secrets.py set servicenow")
        # Web service access only: no UI. Not an internal integration user: those can't hold a password.
        values = {"user_name": user_name, "first_name": person["first_name"], "last_name": person["last_name"],
                  "email": f"{user_name}@example.invalid", "active": "true", "password_needs_reset": "false",
                  "web_service_access_only": "true" if person.get("machine") else "false",
                  "internal_integration_user": "false"}
        # Demos 10 and 19: the records' times read like Slack's and the terminal's.
        if person.get("time_zone"):
            values["time_zone"] = person["time_zone"]
        user = sn.ensure("sys_user", f"user_name={user_name}", values, f"user {user_name}")
        users[user_name] = user["sys_id"]
        if not person["password"]:
            add_roles(sn, user, person["roles"])
            continue
        # Passwords need display-value input. Kept in line with SSM on every run, quietly; a password the
        # instance's policy refuses is dropped without an error, so check it took.
        sn.call("PATCH", f"/api/now/table/sys_user/{user['sys_id']}", {"user_password": password},
                sysparm_input_display_value="true")
        if not sn.find("sys_user", f"sys_id={user['sys_id']}", "user_password").get("user_password"):
            fail(f"ServiceNow refused the password for {user_name}: it doesn't meet the instance's password "
                 f"policy (upper and lower case, a digit, a special character). Set another in SSM.")
        add_roles(sn, user, person["roles"])
    return users


def add_roles(sn, user, roles):
    for role in roles:
        found = sn.find("sys_user_role", f"name={role}", "sys_id")
        if found is None:
            fail(f"This instance has no role {role}. As admin: System Definition, Plugins, find the plugin "
                 f"that provides {role} (for snc_read_only: Read Only Role) and activate it, then run this again.")
        role_id = found["sys_id"]
        sn.ensure("sys_user_has_role", f"user={user['sys_id']}^role={role_id}",
                  {"user": user["sys_id"], "role": role_id}, f"  role {role}")


def get_parameter_or_none(ssm, name):
    try:
        value = ssm.get_parameter(Name=f"/release-demo/{name}", WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        fail(f"/release-demo/{name} doesn't exist yet. Apply infra/ first: scripts/tf.sh apply")
    return None if value == "unset" else value


def setup_groups(sn, users):
    groups = {}
    for name, group in GROUPS.items():
        record = sn.ensure("sys_user_group", f"name={name}", {"name": name, "description": group["description"]},
                           f"group {name}")
        groups[name] = record["sys_id"]
        for member in group["members"]:
            if member not in users:
                print(f"  member {member}: skipped for now ({group.get('optional')})")
                continue
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


def approval_definition(sn, template, name, group):
    return sn.ensure("chg_approval_def", f"name={name}", {
        "name": name, "table": "change_request", "approver_source": template["approver_source"],
        "approval_action": template["approval_action"], "group": group,
        "mandatory": "true", "wait_for": template["wait_for"], "advanced": "false"}, f"approval definition {name}")


def with_clause(condition, clause):
    """A decision's condition with one more clause, before its closing ^EQ."""
    if clause in condition:
        return condition
    base = condition[:-3] if condition.endswith("^EQ") else condition
    return f"{base}^{clause}^EQ"


def setup_approvals(sn, groups, cost=False):
    """One approval, by Change approvers, at Assess. Nothing at Authorize. With cost
    (video 18, once Fiona is set up), the change's Cost review decides whom Assess asks."""
    policy = sn.find("chg_policy", "name=Normal Change Policy", "sys_id")
    if not policy:
        fail("No Normal Change Policy on this instance: the approval steps need the UI. See RUNBOOK.md.")
    template = sn.find("chg_approval_def", "name=CAB Approval")
    definition = approval_definition(sn, template, APPROVAL_DEFINITION, groups["Change approvers"])

    decisions = sn.call("GET", "/api/now/table/sys_decision_question", sysparm_query=f"decision_table={policy['sys_id']}",
                        sysparm_fields="sys_id,label,condition,answer,active,order")
    assess = None
    for decision in decisions:
        if decision["label"] == COST_DECISION:
            continue
        at_assess = "change_request.state=-4" in decision["condition"]
        wanted = ({"answer": definition["sys_id"], "active": "true"} if at_assess else {"active": "false"})
        if at_assess and cost:
            wanted["condition"] = with_clause(decision["condition"], CHANGE_APPROVERS_CLAUSE)
        assess = assess or (decision if at_assess else None)
        changed = {k: v for k, v in wanted.items() if str(raw(decision.get(k, ""))) != v}
        if changed:
            sn.call("PATCH", f"/api/now/table/sys_decision_question/{decision['sys_id']}", changed)
        what = ("asks Change approvers" + (" unless Cost review asks Cost approvers only" if cost else "")) if at_assess else "off"
        print(f"  decision '{decision['label']}': {what}{' (changed)' if changed else ''}")
    if cost:
        cost_definition = approval_definition(sn, template, COST_DEFINITION, groups["Cost approvers"])
        setup_cost_decision(sn, policy["sys_id"], assess, cost_definition["sys_id"])
    return definition


# Video 18: the field Cost review on the change, and the decision that asks Cost approvers.

def setup_cost_field(sn):
    """u_cost_review, a choice next to Type on the change form. False if it needs the UI."""
    field = sn.find("sys_dictionary", f"name=change_request^element={COST_FIELD}", "sys_id")
    if field is None:
        try:
            sn.call("POST", "/api/now/table/sys_dictionary", {
                "name": "change_request", "element": COST_FIELD, "column_label": "Cost review",
                "internal_type": "choice", "max_length": "40", "choice": "1", "active": "true"})
            print(f"  field Cost review ({COST_FIELD}): created")
        except SystemExit:
            print(f"  field Cost review ({COST_FIELD}): the Table API can't make it here.\n{COST_FIELD_STEPS}")
            return False
    else:
        print(f"  field Cost review ({COST_FIELD}): already there")
    for sequence, (value, label) in enumerate(COST_CHOICES, start=1):
        sn.ensure("sys_choice", f"name=change_request^element={COST_FIELD}^value={value}", {
            "name": "change_request", "element": COST_FIELD, "value": value, "label": label,
            "sequence": str(sequence * 10), "language": "en", "inactive": "false"}, f"  choice {label}")
    place_next_to_type(sn)
    return True


def place_next_to_type(sn):
    """Cost review right under Type in the change form's default view."""
    rows = sn.call("GET", "/api/now/table/sys_ui_element", sysparm_fields="sys_id,sys_ui_section,position",
                   sysparm_query="element=type^sys_ui_section.name=change_request^sys_ui_section.view.title=Default view") or []
    if not rows:
        print("  form: Type isn't on the default view here; put Cost review under it by hand (Configure, Form Layout)")
        return
    section, position = raw(rows[0]["sys_ui_section"]), int(rows[0]["position"])
    if not sn.find("sys_ui_element", f"sys_ui_section={section}^element={COST_FIELD}", "sys_id"):
        sn.call("POST", "/api/now/table/sys_ui_element", {"sys_ui_section": section, "element": COST_FIELD,
                                                          "position": str(position + 1)})
    moved = arrange(sn, section, "type", [COST_FIELD])
    print(f"  form: Cost review placed under Type ({moved} positions set)" if moved else "  form: Cost review is under Type")


def arrange(sn, section, anchor, elements):
    """Put elements right after anchor in a form section, in this order, and return how many
    positions it set. A PDI puts an element made through the Table API at the end of its
    section, whatever position it is given, so every position that differs is set again."""
    rows = sorted(sn.call("GET", "/api/now/table/sys_ui_element", sysparm_query=f"sys_ui_section={section}",
                          sysparm_fields="sys_id,element,position", sysparm_limit=500) or [],
                  key=lambda row: int(row["position"]))
    order = [row for row in rows if row["element"] not in elements]
    at = next(index for index, row in enumerate(order) if row["element"] == anchor) + 1
    order[at:at] = [next(row for row in rows if row["element"] == element) for element in elements]
    moved = 0
    for position, row in enumerate(order):
        if int(row["position"]) != position:
            sn.call("PATCH", f"/api/now/table/sys_ui_element/{row['sys_id']}", {"position": str(position)})
            moved += 1
    return moved


def setup_cost_decision(sn, policy_id, assess, definition_id):
    """The decision at Assess that asks Cost approvers when Cost review is set, beside
    the one that asks Change approvers: with "Cost and change approvers" both apply."""
    values = {"decision_table": policy_id, "label": COST_DECISION, "condition": COST_CONDITION,
              "answer": definition_id, "active": "true"}
    existing = sn.find("sys_decision_question", f"decision_table={policy_id}^label={COST_DECISION}")
    if existing:
        changed = {k: v for k, v in values.items() if str(raw(existing.get(k, ""))) != v}
        if changed:
            sn.call("PATCH", f"/api/now/table/sys_decision_question/{existing['sys_id']}", changed)
        print(f"  decision '{COST_DECISION}': asks Cost approvers when Cost review is set{' (changed)' if changed else ''}")
        return
    if assess is None:
        fail("The Normal Change Policy has no decision at Assess to model the cost decision on. See RUNBOOK.md.")
    model = sn.call("GET", f"/api/now/table/sys_decision_question/{assess['sys_id']}") or {}
    copy = {key: raw(value) for key, value in model.items() if not key.startswith("sys_")}
    copy.update(values, order=str(int(raw(model.get("order")) or 100) + 10))
    sn.call("POST", "/api/now/table/sys_decision_question", copy)
    print(f"  decision '{COST_DECISION}': created, asks Cost approvers when Cost review is set")


def setup_emergency_approvals(sn, definition):
    """Demo 19: an emergency change asks Change approvers once, at Authorize, with the
    definition normal changes use at Assess; the policy's other approvals are off."""
    policy = sn.find("chg_policy", "name=Emergency Change Policy", "sys_id")
    if not policy:
        print("  No Emergency Change Policy on this instance: demo 19's emergency changes need the UI steps "
              "in the README (Demo on a call: Argo CD).")
        return
    decisions = sn.call("GET", "/api/now/table/sys_decision_question", sysparm_query=f"decision_table={policy['sys_id']}",
                        sysparm_fields="sys_id,label,condition,answer,active")
    for decision in decisions:
        at_authorize = "change_request.state=-3" in decision["condition"]
        wanted = ({"answer": definition["sys_id"], "active": "true"} if at_authorize else {"active": "false"})
        changed = {k: v for k, v in wanted.items() if str(raw(decision.get(k, ""))) != v}
        if changed:
            sn.call("PATCH", f"/api/now/table/sys_decision_question/{decision['sys_id']}", changed)
        what = "asks Change approvers" if at_authorize else "off"
        print(f"  decision '{decision['label']}': {what}{' (changed)' if changed else ''}")


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


# Video 16: who wrote the release's code and who reviewed it, filled by change.py
# from the merged pull requests, next to Requested by on the change's form.
CHANGE_FIELDS = {"u_code_written_by": "Code written by", "u_code_reviewed_by": "Code reviewed by"}
FORM_STEPS = ("As admin, open any change, then the form's menu, Configure, Form Layout: move Code written by and "
              "Code reviewed by to just below Requested by, and Save.")


def setup_change_fields(sn):
    for element, label in CHANGE_FIELDS.items():
        if sn.find("sys_dictionary", f"name=change_request^element={element}", "sys_id"):
            print(f"  field {label}: already there")
            continue
        sn.call("POST", "/api/now/table/sys_dictionary", {
            "name": "change_request", "element": element, "column_label": label, "internal_type": "string",
            "max_length": "200", "active": "true", "read_only": "false"})
        print(f"  field {label}: created")
    place_on_form(sn)


def place_on_form(sn):
    """On the default view, right after Requested by."""
    section = sn.find("sys_ui_section", "name=change_request^view=Default view^captionISEMPTY", "sys_id")
    if section is None:
        print(f"  form: no default section found. {FORM_STEPS}")
        return
    rows = sn.call("GET", "/api/now/table/sys_ui_element", sysparm_query=f"sys_ui_section={section['sys_id']}",
                   sysparm_fields="sys_id,element,position", sysparm_limit=500) or []
    placed = {row["element"] for row in rows}
    if "requested_by" not in placed:
        print(f"  form: Requested by isn't on the default form. {FORM_STEPS}")
        return
    for element in CHANGE_FIELDS:
        if element not in placed:
            sn.call("POST", "/api/now/table/sys_ui_element", {"sys_ui_section": section["sys_id"], "element": element,
                                                              "position": str(len(rows))})
    moved = arrange(sn, section["sys_id"], "requested_by", list(CHANGE_FIELDS))
    print(f"  form: Code written by and Code reviewed by placed below Requested by ({moved} positions set)" if moved
          else "  form: Code written by and Code reviewed by are below Requested by")


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


def approvers_at_assess(sn, change_id):
    approvals = sn.call("GET", "/api/now/table/sysapproval_approver", sysparm_query=f"sysapproval={change_id}",
                        sysparm_fields="sys_id,approver,state", sysparm_display_value="true") or []
    return approvals, sorted({a["approver"]["display_value"] for a in approvals})


def change_state(sn, change_id, wanted, tries=10):
    state = None
    for _ in range(tries):
        time.sleep(3)
        state = sn.call("GET", f"/api/now/table/change_request/{change_id}", sysparm_fields="state",
                        sysparm_display_value="true")["state"]
        if state == wanted:
            break
    return state


def approve(sn, approvals, name):
    for approval in approvals:
        if approval["approver"]["display_value"] == name:
            sn.call("PATCH", f"/api/now/table/sysapproval_approver/{approval['sys_id']}", {"state": "approved"})


def prove_cost_routes(sn, users, groups, items):
    """Video 18's three routes, each with a test change cancelled right after: Cost
    review empty asks Alex (prove_approvals), "Cost approvers" asks Fiona only,
    "Cost and change approvers" asks both and needs both."""
    problems = []
    for value, wanted in (("cost_approvers", ["Fiona Finance"]),
                          ("cost_and_change_approvers", ["Alex Change Manager", "Fiona Finance"])):
        change = sn.call("POST", "/api/sn_chg_rest/change/normal", {
            "short_description": f"Cost review check by servicenow_setup.py ({value}): cancelled right after",
            "assignment_group": groups["Platform team"], "requested_by": users["sam.developer"],
            "cmdb_ci": next(iter(items.values())), COST_FIELD: value})
        change_id, number = change["sys_id"]["value"], change["number"]["value"]
        sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": ASSESS})
        approvals, asked = approvers_at_assess(sn, change_id)
        result = "not checked"
        if asked == wanted:
            approve(sn, approvals, wanted[0])
            if len(wanted) > 1:
                early = change_state(sn, change_id, "Scheduled", tries=2)
                if early == "Scheduled":
                    problems.append(f"{value}: Scheduled after {wanted[0]} alone; both must approve")
                approve(sn, approvals, wanted[1])
            result = change_state(sn, change_id, "Scheduled")
        sn.call("PATCH", f"/api/sn_chg_rest/change/normal/{change_id}", {"state": CANCELED,
                "close_notes": "Cost review check by servicenow_setup.py."})
        print(f"  test change {number} ({value}): asked {asked or 'nobody'}; after approval: {result}; cancelled")
        if asked != wanted:
            problems.append(f"{value}: asked {asked or 'nobody'}, not {wanted}")
        elif result != "Scheduled":
            problems.append(f"{value}: {result} after approval, not Scheduled")
    if problems:
        fail("The cost routes aren't right: " + "; ".join(problems) + ". See RUNBOOK.md for the UI steps.")


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
    print("Cost review (video 18)")
    cost = "fiona.finance" in users and setup_cost_field(sn)
    if "fiona.finance" not in users:
        print("  waits for Fiona's password: set servicenow/fiona-password, then run this again")
    print("Normal change approval")
    definition = setup_approvals(sn, groups, cost=cost)
    print("Emergency change approval")
    setup_emergency_approvals(sn, definition)
    print("Standard change templates")
    setup_templates(sn, groups, items)
    print("Change fields")
    setup_change_fields(sn)
    print("Pipeline access")
    prove_access(sn, ssm)
    # Demos 14 and 22: runs on GitHub Actions and Azure Pipelines wait on the approval bridge.
    print("Approval bridge")
    approval_bridge.setup(sn, ssm)
    if args.test:
        print("Approval check")
        prove_approvals(sn, users, groups, items)
        prove_standard(sn, ssm)
        if cost:
            print("Cost review check (video 18)")
            prove_cost_routes(sn, users, groups, items)
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
