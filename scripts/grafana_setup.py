"""Put the release dashboards and the CloudWatch data source on the Grafana Cloud stack.

    python scripts/grafana_setup.py            set up, then check every panel's queries
    python scripts/grafana_setup.py --check    only the check

Idempotent, so it runs again after every change to grafana/:

- the folder "Release demo";
- the data source "CloudWatch (release-demo)", through Grafana Assume Role,
  once Terraform has made its role (infra/grafana.tf). Without the role it is
  skipped, and the Lambda panels stay empty;
- every dashboard in grafana/*.json, saved only when it changed, so a second
  run adds no new version.

The check runs each panel's queries through Grafana, as the dashboard does,
and prints one line per query: ok, none (no data yet) or FAIL.

Token: grafana/setup-token, a service account with the Admin role, when it is
set. Otherwise grafana/service-account-token, the pipeline's Editor token for
annotations: it can save dashboards, but not data sources. Prints no token and
no value from SSM except the dashboard's address.

A demo adds its service as a row to grafana/releases.json: copy the
orders-service row's panels, change service="orders-service" in the queries,
the panel ids and the annotation tags. A demo that needs a view of its own
(demo 1's Store fleet) adds a dashboard next to it, uid release-demo-<name>.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

from common import REGION, ROOT, SSM_PREFIX, aws_session, fail, get_parameter, ssm_client

USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"
FOLDER = {"uid": "release-demo-dashboards", "title": "Release demo"}
ROLE_NAME = "release-demo-grafana-cloudwatch"  # infra/grafana.tf
CLOUDWATCH_UID = "release-demo-cloudwatch"

TOKEN_STEPS = """\
  The token can't write data sources: that needs the Admin role. Once:
  1. Grafana: Administration, Users and access, Service accounts, Add service account.
     Display name release-demo-setup, role Admin, Create.
  2. Add service account token, Generate token, copy it (glsa_...).
  3. .venv/bin/python scripts/secrets.py set grafana/setup-token
  4. Run this script again."""

ROLE_STEPS = """\
  Terraform makes the role once it knows Grafana's AWS account. In Grafana:
  Connections, Data sources, the CloudWatch one you opened for the External ID
  (or Add new data source, CloudWatch), Settings, Authentication provider
  Grafana Assume Role. Its instructions box shows the AWS account id to trust.
  Put it in infra/local.auto.tfvars as grafana_aws_account_id = "<id>", run
  scripts/tf.sh apply, then run this script again."""


class Grafana:
    """The stack's HTTP API with one token. Errors come back as (status, body)."""

    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"message": raw[:300]}

    def must(self, method, path, body=None, ok=(200,)):
        status, data = self.call(method, path, body)
        if status not in ok:
            fail(f"Grafana answered {status} to {method} {path}: {message(data)}")
        return status, data


def message(data):
    return data.get("message", json.dumps(data)[:300]) if isinstance(data, dict) else str(data)[:300]


def optional_parameter(ssm, name):
    """A parameter's value, or None while it is missing or unset."""
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + name, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    return None if value == "unset" else value


def dashboards():
    return [json.loads(path.read_text()) for path in sorted((ROOT / "grafana").glob("*.json"))]


def ensure_folder(grafana):
    # The folder list, not GET /api/folders/<uid>: an Editor token gets 403, not
    # 404, for a folder that doesn't exist yet.
    _, folders = grafana.must("GET", "/api/folders?limit=1000")
    if any(folder["uid"] == FOLDER["uid"] for folder in folders):
        return
    status, data = grafana.call("POST", "/api/folders", FOLDER)
    if status != 200:
        fail(f"Grafana answered {status} creating the folder {FOLDER['title']}: {message(data)}")
    print(f"Folder {FOLDER['title']}: created")


def cloudwatch_role_arn():
    """The Grafana Assume Role role's ARN, or None until Terraform has made it."""
    iam = aws_session().client("iam")
    try:
        return iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        return None


def cloudwatch_body(role_arn):
    # usePerDatasourceExternalId false: the stack's external id, which the
    # role's trust policy expects (grafana/external-id), not a per-data-source one.
    return {"uid": CLOUDWATCH_UID, "name": "CloudWatch (release-demo)", "type": "cloudwatch", "access": "proxy",
            "jsonData": {"authType": "grafana_assume_role", "assumeRoleArn": role_arn,
                         "defaultRegion": REGION, "usePerDatasourceExternalId": False}}


def datasource_unchanged(existing, wanted):
    return (existing.get("name") == wanted["name"]
            and all(existing.get("jsonData", {}).get(key) == value for key, value in wanted["jsonData"].items()))


def ensure_cloudwatch(grafana):
    role_arn = cloudwatch_role_arn()
    if not role_arn:
        print(f"CloudWatch: skipped, the IAM role {ROLE_NAME} doesn't exist yet.\n{ROLE_STEPS}")
        return False
    wanted = cloudwatch_body(role_arn)
    status, existing = grafana.call("GET", f"/api/datasources/uid/{CLOUDWATCH_UID}")
    if status == 200 and datasource_unchanged(existing, wanted):
        print(f"CloudWatch: {wanted['name']} unchanged")
    else:
        if status == 200:
            status, data = grafana.call("PUT", f"/api/datasources/uid/{CLOUDWATCH_UID}", wanted)
        else:
            status, data = grafana.call("POST", "/api/datasources", wanted)
        if status in (401, 403):
            print(f"CloudWatch: not saved ({status}).\n{TOKEN_STEPS}")
            return False
        if status != 200:
            fail(f"Grafana answered {status} for the CloudWatch data source: {message(data)}")
        print(f"CloudWatch: {wanted['name']} saved")
    status, health = grafana.call("GET", f"/api/datasources/uid/{CLOUDWATCH_UID}/health")
    print(f"CloudWatch: health {health.get('status', status) if isinstance(health, dict) else status}: {message(health)}")
    return True


def comparable(dashboard):
    """A dashboard without what Grafana adds on save."""
    return {key: value for key, value in dashboard.items() if key not in ("id", "version")}


def ensure_dashboard(grafana, dashboard):
    status, existing = grafana.call("GET", f"/api/dashboards/uid/{dashboard['uid']}")
    if (status == 200 and existing["meta"].get("folderUid") == FOLDER["uid"]
            and comparable(existing["dashboard"]) == comparable(dashboard)):
        print(f"Dashboard {dashboard['title']}: unchanged")
        return
    if status not in (200, 404):
        fail(f"Grafana answered {status} for the dashboard {dashboard['uid']}: {message(existing)}")
    status, data = grafana.call("POST", "/api/dashboards/db", {
        "dashboard": {**dashboard, "id": None}, "folderUid": FOLDER["uid"], "overwrite": True,
        "message": "scripts/grafana_setup.py"})
    if status in (401, 403):
        fail(f"Dashboard {dashboard['title']}: not saved ({status}: {message(data)}). Saving dashboards needs\n"
             "  the Editor role: Administration, Users and access, Service accounts, release-pipeline, Role.")
    if status != 200:
        fail(f"Grafana answered {status} for the dashboard {dashboard['uid']}: {message(data)}")
    print(f"Dashboard {dashboard['title']}: saved, version {data.get('version')}")


def resolve_datasources(dashboard, available):
    """Dashboard variable name to data source: its saved choice if it exists,
    else the first data source of its type, as Grafana picks for a hidden variable."""
    by_uid = {source["uid"]: source for source in available}
    chosen = {}
    for variable in dashboard.get("templating", {}).get("list", []):
        if variable.get("type") != "datasource":
            continue
        current = variable.get("current", {}).get("value")
        of_type = [source for source in available if source["type"] == variable["query"]]
        chosen[variable["name"]] = by_uid.get(current) or (of_type[0] if of_type else None)
    return chosen


def summary(frames):
    """A short description of what a query returned: rows of logs, or the last values."""
    rows, values = 0, []
    for frame in frames:
        fields = frame["schema"]["fields"]
        columns = frame.get("data", {}).get("values", [])
        names = [field["name"] for field in fields]
        if "Line" in names:
            lines = columns[names.index("Line")] if columns else []
            rows += len(lines)
            values += [line for line in lines[:1] if not line.startswith("{")]  # a version, not an event
            continue
        for field, column in zip(fields, columns):
            if field.get("type") == "number" and column:
                last = [value for value in column if value is not None]
                values.append(round(last[-1], 2) if last else None)
    if rows:
        return f"{rows} log lines, newest: {values[0]}" if values else f"{rows} log lines"
    return ", ".join(map(str, values)) if values else ""


def check(grafana, dashboard):
    """Every panel's queries, run through Grafana. Returns the number of failures."""
    _, available = grafana.must("GET", "/api/datasources")
    chosen = resolve_datasources(dashboard, available)
    failures = 0
    for panel in dashboard["panels"]:
        targets = panel.get("targets")
        if not targets:
            continue
        queries, missing = [], None
        for target in targets:
            source = dict(target["datasource"])
            if source["uid"].startswith("${"):
                picked = chosen.get(source["uid"][2:-1])
                if not picked or not picked.get("jsonData"):
                    # A data source added in the UI and never saved has no settings.
                    missing = f"no {source['type']} data source set up yet" + (
                        f" ({picked['name']} has no settings)" if picked else "")
                    break
                source["uid"] = picked["uid"]
            queries.append({**target, "datasource": source})
        if missing:
            print(f"none {panel['title']}: {missing}")
            continue
        start = f"now-{panel['timeFrom']}" if panel.get("timeFrom") else dashboard["time"]["from"]
        status, data = grafana.call("POST", "/api/ds/query", {"queries": queries, "from": start, "to": "now"})
        results = data.get("results", {}) if isinstance(data, dict) else {}
        for query in queries:
            result = results.get(query["refId"], {})
            error = result.get("error") or (message(data) if not results else None)
            described = summary(result.get("frames", []))
            if error:
                failures += 1
                print(f"FAIL {panel['title']} {query['refId']}: {error[:200]}")
            elif described:
                print(f"ok   {panel['title']} {query['refId']}: {described}")
            else:
                print(f"none {panel['title']} {query['refId']}: no data yet")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only run every panel's queries")
    args = parser.parse_args()

    ssm = ssm_client()
    url = get_parameter(ssm, "grafana/stack-url")
    token = optional_parameter(ssm, "grafana/setup-token") or get_parameter(ssm, "grafana/service-account-token")
    grafana = Grafana(url, token)

    if not args.check:
        ensure_folder(grafana)
        ensure_cloudwatch(grafana)
        for dashboard in dashboards():
            ensure_dashboard(grafana, dashboard)

    failures = 0
    for dashboard in dashboards():
        print(f"\n{dashboard['title']}: {grafana.url}/d/{dashboard['uid']}")
        failures += check(grafana, dashboard)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
