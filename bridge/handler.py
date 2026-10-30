"""The approval bridge: production waits in GitHub Actions or Azure Pipelines
until ServiceNow says the change is approved, and nobody in GitHub or Azure
DevOps can approve it instead.

One Lambda function behind a public function URL (infra/approval_bridge.tf),
with three routes:

- POST /github: GitHub's deployment_protection_rule webhook, sent to the
  release-demo GitHub App when a job of a run waits on an environment whose
  protection rule is the app. Its X-Hub-Signature-256 is checked against the
  webhook secret in SSM. The bridge finds the change whose correlation id names
  the run, github-actions:<repository>:<run id> (change.py writes it), and
  answers on the run: approved at once if the change is approved already, a
  status report if it waits, rejected if there is no change.
- POST /servicenow: the business rule "release-demo: approval bridge" calls it,
  with the bearer token kept in SSM and in a ServiceNow system property, when
  a change's approval turns approved or rejected. The bridge trusts nothing in
  the call's body but the change's sys_id: it reads the change itself, as
  release.pipeline, and approves or rejects the run its correlation id names.
- POST /azure-devops/check (demo 22): the delivery of production's Invoke REST
  API check, in callback mode. Nothing in it is trusted: its PlanUrl must be
  the organization's, and its AuthToken, the job access token Azure DevOps
  issued for the check, must read the run, which must be the release
  pipeline's, from main. The bridge finds the change whose correlation id,
  azure-pipelines:<organization>/<project>:<run id>, names the run. Decided
  already, it completes the check at once; waiting, it keeps the callback in
  the pending-checks table (DynamoDB, 48 hours, the token's life) and writes
  the waiting line to the check's log; ServiceNow's call then completes it
  with the stored token.

The rule is change.py wait's: an approval counts only from someone other than
the person who asked, whatever ServiceNow says. Every route answers at once and
does the rest in an asynchronous call to itself, as the Slack handler does:
GitHub waits 10 seconds for a webhook's answer, Azure DevOps 3, and
ServiceNow's business rule runs inside Alex's click.

GitHub sees the app's answers as comments on the run, at most 10 status reports
per deployment and 1,024 characters each; Azure DevOps shows the latest line
of the check's log on the run page. The texts below are the ones the
video scripts quote. The app's JWT is signed with PyJWT and cryptography, which
bridge/build.sh installs for Lambda's platform; the rest is the standard
library and boto3. Secrets come from SSM and are never logged.
"""

import base64
import calendar
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

SSM_PREFIX = "/release-demo/"
CACHE_SECONDS = 5 * 60
USER_AGENT = "release-demo-approval-bridge/1.0 (+https://github.com/m-atek/release-demo)"
GITHUB_API = "https://api.github.com"
# The environments whose jobs wait for the bridge: production, and bridge-test
# for the first check of the setup (scripts/demos/github_actions/bridge_test.py).
ENVIRONMENTS = ("production", "bridge-test")
GITHUB = "github-actions:"
AZURE = "azure-pipelines:"
CLOSED, CANCELED = "3", "4"
# The Invoke REST API check's headers, as setup.py configures them.
CHECK_HEADERS = ("planurl", "projectid", "hubname", "planid", "jobid", "timelineid", "taskinstanceid", "authtoken",
                 "buildid")
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
CHECKS_TABLE = os.environ.get("CHECKS_TABLE", "release-demo-approval-bridge-checks")
TOKEN_SECONDS = 48 * 3600  # how long Azure DevOps' job access token for a check lives

WAITING = "ServiceNow change {number} is waiting for approval."
APPROVED = "Approved by {name} in ServiceNow. Change {number}."
STANDARD = "Pre-approved in ServiceNow as a standard change. Change {number}."
REJECTED = "Rejected by {name} in ServiceNow. Change {number}."
REJECTED_UNNAMED = "Rejected in ServiceNow. Change {number}."
REFUSED = ("Approved by the person who asked for the release. Refused: the person who asks can't approve. "
           "Change {number}.")
NO_CHANGE = "No ServiceNow change for this run."
NOT_OPEN = "ServiceNow change {number} is no longer open."
BRANCH = "Only main deploys to production. This run is from {branch}."
ASLEEP = "ServiceNow is asleep: wake it at developer.servicenow.com, and the bridge answers when the change is approved."

GITHUB_PARAMETERS = ["github/app-id", "github/app-private-key", "github/repository"]
SERVICENOW_PARAMETERS = ["servicenow/instance-url", "servicenow/release-pipeline-password"]


class Asleep(Exception):
    """The ServiceNow PDI hibernates when idle."""


def handler(event, context):
    if "work" in event:  # the asynchronous call: only this function's role can send it
        run(event["work"])
        return None
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    headers = {name.lower(): value for name, value in (event.get("headers") or {}).items()}
    if method != "POST":
        return http(405)
    if path == "/github":
        return from_github(headers, body, context)
    if path == "/servicenow":
        return from_servicenow(headers, body, context)
    if path == "/azure-devops/check":
        return from_azure(headers, context)
    return http(404)


# The first call: check, answer, hand over.

def from_github(headers, body, context):
    secret = parameters("bridge/github-webhook-secret")["bridge/github-webhook-secret"]
    if not secret:
        log("refused: bridge/github-webhook-secret isn't set in SSM")
        return http(500)
    if not signed_by_github(secret, headers.get("x-hub-signature-256", ""), body):
        log("refused: bad or missing X-Hub-Signature-256")
        return http(401)
    event_name = headers.get("x-github-event", "")
    if event_name == "ping":
        return http(200)
    if event_name != "deployment_protection_rule":
        log(f"ignored: {event_name or 'no'} event")
        return http(202)
    payload = json.loads(body)
    run_id = run_of(payload.get("deployment_callback_url", ""))
    environment = payload.get("environment", "")
    repository = (payload.get("repository") or {}).get("full_name", "")
    if payload.get("action") != "requested" or not run_id or environment not in ENVIRONMENTS:
        log(f"ignored: {payload.get('action')} for {environment or 'no environment'}, run {run_id or 'unknown'}")
        return http(202)
    if repository != parameters("github/repository")["github/repository"]:
        log(f"ignored: a request from {repository}")
        return http(202)
    work = {"route": "github", "run_id": run_id, "environment": environment, "ref": payload.get("ref") or ""}
    aws("lambda").invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                         Payload=json.dumps({"work": work}).encode())
    log(f"run {run_id} waits on {environment}: handed over")
    return http(202)


def signed_by_github(secret, signature, body):
    """GitHub signs the body with the webhook secret: sha256=<HMAC-SHA256 hex>."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def run_of(callback_url):
    """The run id in .../actions/runs/<id>/deployment_protection_rule, or ''."""
    found = re.fullmatch(r"https://api\.github\.com/repos/[^/]+/[^/]+/actions/runs/(\d+)/deployment_protection_rule",
                         callback_url or "")
    return found.group(1) if found else ""


def from_servicenow(headers, body, context):
    token = parameters("bridge/servicenow-token")["bridge/servicenow-token"]
    if not token:
        log("refused: bridge/servicenow-token isn't set in SSM")
        return http(500)
    if not hmac.compare_digest(headers.get("authorization", ""), f"Bearer {token}"):
        log("refused: bad or missing bearer token")
        return http(401)
    try:
        sys_id = json.loads(body or b"{}").get("sys_id", "")
    except ValueError:
        sys_id = ""
    if not re.fullmatch(r"[0-9a-f]{32}", sys_id or ""):
        log("refused: no change sys_id in the call")
        return http(400)
    aws("lambda").invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                         Payload=json.dumps({"work": {"route": "servicenow", "sys_id": sys_id}}).encode())
    log(f"change {sys_id}: approval changed, handed over")
    return http(202)


def from_azure(headers, context):
    """The check's delivery. Answered within Azure DevOps' 3 seconds; the rest
    is asynchronous. A call for another organization, or without the run's ids,
    gets no decision: only a token Azure DevOps issued can give one."""
    check = {name: headers.get(name, "") for name in CHECK_HEADERS}
    organization = parameters("azure-devops/organization")["azure-devops/organization"]
    if not organization or check["planurl"].rstrip("/") != f"https://dev.azure.com/{organization}":
        log("refused: a check call for another organization")
        return http(403)
    ids = ("projectid", "planid", "jobid", "timelineid", "taskinstanceid")
    if (not all(GUID.fullmatch(check[name]) for name in ids) or not check["buildid"].isdigit()
            or not re.fullmatch(r"\w+", check["hubname"]) or not check["authtoken"]):
        log("refused: a check call without the run's ids and token")
        return http(400)
    aws("lambda").invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                         Payload=json.dumps({"work": {"route": "azure", "check": check}}).encode())
    log(f"run {check['buildid']}: production's check delivered, handed over")
    return http(200)


# The asynchronous call: ServiceNow's answer, given to the run.

def run(work):
    """Never raises: Lambda would retry the event, and a retry could post twice."""
    try:
        if work.get("route") == "github":
            github_requested(work)
        elif work.get("route") == "servicenow":
            servicenow_decided(work)
        elif work.get("route") == "azure":
            azure_delivered(work["check"])
        else:
            log(f"ignored: work for {work.get('route')}")
    except Exception as error:  # the log has the detail
        log(f"failed: {error.__class__.__name__}: {error}")


def github_requested(work):
    repository = parameters("github/repository")["github/repository"]
    run_id, environment = work["run_id"], work["environment"]
    branch = work.get("ref", "").removeprefix("refs/heads/")
    if branch and branch != "main":
        review(repository, run_id, environment, "rejected", BRANCH.format(branch=branch))
        return
    try:
        sn = ServiceNow.connect()
        change = sn.change_by_correlation(f"{GITHUB}{repository}:{run_id}")
        verdict, text = decide(change, sn.approvals(change) if change else [], sn.first_name)
    except Asleep:
        review(repository, run_id, environment, None, ASLEEP)
        return
    review(repository, run_id, environment, None if verdict == "waiting" else verdict, text)


def servicenow_decided(work):
    sn = ServiceNow.connect()
    change = sn.change(work["sys_id"])
    if not change:
        log(f"change {work['sys_id']}: not found")
        return
    correlation = change.get("correlation_id") or ""
    if correlation.startswith(AZURE):
        azure_decided(sn, change)
        return
    if not correlation.startswith(GITHUB):
        log(f"{change['number']}: correlation id {correlation or 'empty'}, nothing to tell")
        return
    repository, _, run_id = correlation.removeprefix(GITHUB).rpartition(":")
    if repository != parameters("github/repository")["github/repository"] or not run_id.isdigit():
        log(f"{change['number']}: {correlation} isn't a run of this repository")
        return
    verdict, text = decide(change, sn.approvals(change), sn.first_name)
    if verdict == "waiting":
        log(f"{change['number']}: still waiting")
        return
    for environment in waiting_environments(repository, run_id):
        review(repository, run_id, environment, verdict, text)


def azure_delivered(check):
    """Production's check, delivered: prove the token, find the change, answer
    or wait. A token that can't read the run is forged or expired: no decision."""
    devops = AzureDevOps(check)
    build = devops.build()
    if build is None:
        log(f"run {check['buildid']}: the check's token doesn't read the run, so no decision")
        return
    if str(build.get("definition", {}).get("id")) != parameters("azure-devops/pipeline-id")["azure-devops/pipeline-id"]:
        devops.complete("failed", NO_CHANGE)
        return
    if build.get("sourceBranch") != "refs/heads/main":
        devops.complete("failed", BRANCH.format(branch=build.get("sourceBranch", "").removeprefix("refs/heads/")))
        return
    organization = parameters("azure-devops/organization")["azure-devops/organization"]
    correlation = f"{AZURE}{organization}/{build['project']['name']}:{check['buildid']}"
    try:
        sn = ServiceNow.connect()
        change = sn.change_by_correlation(correlation)
        verdict, text = decide(change, sn.approvals(change) if change else [], sn.first_name)
    except Asleep:
        verdict, text = "waiting", ASLEEP
    if verdict != "waiting":
        devops.complete("succeeded" if verdict == "approved" else "failed", text)
        return
    PendingChecks().keep(correlation, check, devops.say(text))


def azure_decided(sn, change):
    """ServiceNow decided: complete the run's waiting check with its stored token."""
    pending = PendingChecks()
    item = pending.get(change["correlation_id"])
    if item is None:
        log(f"{change['number']}: no check waits for it")
        return
    verdict, text = decide(change, sn.approvals(change), sn.first_name)
    if verdict == "waiting":
        log(f"{change['number']}: still waiting")
        return
    AzureDevOps(item["check"], log_id=item.get("log_id")).complete("succeeded" if verdict == "approved" else "failed", text)
    pending.forget(change["correlation_id"])


class AzureDevOps:
    """A run's check, with the job access token Azure DevOps issued for it."""

    def __init__(self, check, log_id=None):
        self.check, self.log_id = check, log_id
        self.plan = (f"{check['planurl'].rstrip('/')}/{check['projectid']}/_apis/distributedtask/hubs/"
                     f"{check['hubname']}/plans/{check['planid']}")

    def call(self, method, url, body=None, content_type="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(url, method=method, data=data, headers={
            "Authorization": f"Bearer {self.check['authtoken']}", "Content-Type": content_type,
            "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as error:
            log(f"Azure DevOps {method} {url.split('/_apis/')[-1].split('?')[0]}: {error.code}")
            return error.code, None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            log(f"Azure DevOps {method}: {error.__class__.__name__}")
            return 0, None

    def build(self):
        url = (f"{self.check['planurl'].rstrip('/')}/{self.check['projectid']}/_apis/build/builds/"
               f"{self.check['buildid']}?api-version=7.1")
        status, reply = self.call("GET", url)
        return reply if status == 200 else None

    def say(self, line):
        """One line in the check's log, which the run page shows as its latest:
        a log is made and attached to the check's timeline record the first time."""
        if self.log_id is None:
            status, reply = self.call("POST", f"{self.plan}/logs?api-version=7.1",
                                      {"path": f"logs\\{self.check['taskinstanceid']}"})
            if status != 200 or not reply:
                return None
            self.log_id = reply["id"]
            self.call("PATCH", f"{self.plan}/timelines/{self.check['timelineid']}/records?api-version=7.1",
                      {"value": [{"id": self.check["taskinstanceid"], "log": {"id": self.log_id}}], "count": 1})
        self.call("POST", f"{self.plan}/logs/{self.log_id}?api-version=7.1", (line + "\n").encode(),
                  content_type="application/octet-stream")
        log(f"run {self.check['buildid']}: {line}")
        return self.log_id

    def complete(self, result, line):
        """The line, then the decision: the Post Event API's TaskCompleted."""
        self.say(line)
        status, _ = self.call("POST", f"{self.plan}/events?api-version=7.1", {
            "name": "TaskCompleted", "taskId": self.check["taskinstanceid"], "jobId": self.check["jobid"],
            "result": result})
        log(f"run {self.check['buildid']}: check {result} ({status})")
        return status


class PendingChecks:
    """The checks that wait for ServiceNow, by the change's correlation id, each
    kept 48 hours, the life of its token. The token never leaves the table and
    this function, and is never logged."""

    def keep(self, correlation, check, log_id):
        item = {"correlation_id": {"S": correlation}, "check": {"S": json.dumps(check)},
                "expires_at": {"N": str(int(time.time()) + TOKEN_SECONDS)}}
        if log_id is not None:
            item["log_id"] = {"N": str(log_id)}
        aws("dynamodb").put_item(TableName=CHECKS_TABLE, Item=item)
        log(f"{correlation}: waits for ServiceNow")

    def get(self, correlation):
        item = aws("dynamodb").get_item(TableName=CHECKS_TABLE, Key={"correlation_id": {"S": correlation}},
                                        ConsistentRead=True).get("Item")
        if not item or int(item["expires_at"]["N"]) < time.time():
            return None
        return {"check": json.loads(item["check"]["S"]),
                "log_id": int(item["log_id"]["N"]) if "log_id" in item else None}

    def forget(self, correlation):
        aws("dynamodb").delete_item(TableName=CHECKS_TABLE, Key={"correlation_id": {"S": correlation}})


def decide(change, approvals, first_name):
    """The bridge's answer for a run's change, as change.py wait decides:
    ("approved" | "rejected" | "waiting", the comment). An approval by the
    person who asked is refused, whatever ServiceNow says."""
    if not change:
        return "rejected", NO_CHANGE
    number = change["number"]
    if change.get("state") in (CLOSED, CANCELED):
        return "rejected", NOT_OPEN.format(number=number)
    if change.get("approval") == "rejected":
        rejecters = [a["approver"] for a in approvals if a["state"] == "rejected"]
        name = first_name(rejecters[0]) if rejecters else ""
        return "rejected", (REJECTED.format(name=name, number=number) if name else REJECTED_UNNAMED.format(number=number))
    approvers = [a["approver"] for a in approvals if a["state"] == "approved"]
    if change.get("requested_by") and change["requested_by"] in approvers:
        return "rejected", REFUSED.format(number=number)
    if change.get("approval") == "approved":
        if approvers:
            return "approved", APPROVED.format(name=first_name(approvers[0]), number=number)
        if change.get("type") == "standard":
            return "approved", STANDARD.format(number=number)
    return "waiting", WAITING.format(number=number)


# ServiceNow, read as release.pipeline.

class ServiceNow:
    FIELDS = "sys_id,number,approval,state,type,requested_by,correlation_id"

    def __init__(self, url, password):
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"release.pipeline:{password}".encode()).decode()

    @classmethod
    def connect(cls):
        values = parameters(*SERVICENOW_PARAMETERS)
        return cls(values["servicenow/instance-url"], values["servicenow/release-pipeline-password"])

    def get(self, path, **query):
        query["sysparm_exclude_reference_link"] = "true"
        request = urllib.request.Request(f"{self.url}{path}?{urllib.parse.urlencode(query)}", headers={
            "Authorization": self.auth, "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            if "hibernat" in raw.lower():
                raise Asleep() from error
            raise RuntimeError(f"ServiceNow GET {path}: {error.code}") from error
        if "hibernat" in raw.lower() or raw.lstrip().startswith("<"):
            raise Asleep()
        return json.loads(raw)["result"]

    def change(self, sys_id):
        rows = self.get("/api/now/table/change_request", sysparm_query=f"sys_id={sys_id}", sysparm_limit=1,
                        sysparm_fields=self.FIELDS)
        return rows[0] if rows else None

    def change_by_correlation(self, correlation):
        rows = self.get("/api/now/table/change_request", sysparm_query=f"correlation_id={correlation}^ORDERBYDESCsys_created_on",
                        sysparm_limit=1, sysparm_fields=self.FIELDS)
        return rows[0] if rows else None

    def approvals(self, change):
        return self.get("/api/now/table/sysapproval_approver",
                        sysparm_query=f"sysapproval={change['sys_id']}^stateINapproved,rejected",
                        sysparm_fields="state,approver")

    def first_name(self, user_id):
        rows = self.get("/api/now/table/sys_user", sysparm_query=f"sys_id={user_id}", sysparm_limit=1,
                        sysparm_fields="first_name,name")
        return (rows[0].get("first_name") or rows[0].get("name")) if rows else ""


# GitHub, as the release-demo GitHub App.

def review(repository, run_id, environment, state, comment):
    """Approve or reject the environment's protection rule on the run, with a
    comment, or with state None post the comment alone as a status report."""
    body = {"environment_name": environment, "comment": comment[:1024]}
    if state:
        body["state"] = state
    status = github("POST", f"/repos/{repository}/actions/runs/{run_id}/deployment_protection_rule", body)
    log(f"run {run_id} on {environment}: {state or 'report'} ({status}): {comment}")
    return status


def waiting_environments(repository, run_id):
    """The run's environments waiting on the app, or, where GitHub doesn't list
    them, every environment the bridge answers for: a rule not waiting
    refuses the answer, and nothing changes."""
    try:
        pending = github("GET", f"/repos/{repository}/actions/runs/{run_id}/pending_deployments")
    except RuntimeError as error:
        log(f"run {run_id}: no pending deployments listed ({error})")
        pending = []
    names = [p.get("environment", {}).get("name") for p in pending or []]
    return [name for name in names if name in ENVIRONMENTS] or list(ENVIRONMENTS)


def github(method, path, body=None, attempts=3):
    """A REST call with the app's installation token. Returns the reply's JSON,
    or its status for a reply without one. A refused review raises nothing:
    its status is logged."""
    token = installation_token()
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 ** attempt)  # GitHub didn't answer, or answered 5xx: twice more
        status, reply = call_github(method, path, f"token {token}", body)
        if 0 < status < 500:
            break
    if method == "GET":
        if status != 200:
            raise RuntimeError(f"GitHub {method} {path}: {status}")
        return reply
    return status


def call_github(method, path, authorization, body=None):
    request = urllib.request.Request(GITHUB_API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None, headers={
                                         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                         "Authorization": authorization, "User-Agent": USER_AGENT,
                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            detail = json.loads(raw).get("message", "")
        except ValueError:
            detail = ""
        log(f"GitHub {method} {path}: {error.code} {detail}")
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        log(f"GitHub {method} {path}: {error.__class__.__name__}")
        return 0, None


_token = {}


def installation_token():
    """An installation token for the repository, with Deployments write and
    Actions read only, kept until five minutes before it expires."""
    if _token.get("value") and _token["expires"] - 300 > time.time():
        return _token["value"]
    values = parameters(*GITHUB_PARAMETERS)
    jwt = app_jwt(values["github/app-id"], values["github/app-private-key"])
    repository = values["github/repository"]
    status, installation = call_github("GET", f"/repos/{repository}/installation", f"Bearer {jwt}")
    if status != 200:
        raise RuntimeError(f"the GitHub App isn't installed on {repository} ({status})")
    status, token = call_github("POST", f"/app/installations/{installation['id']}/access_tokens", f"Bearer {jwt}", {
        "repositories": [repository.split("/", 1)[1]], "permissions": {"deployments": "write", "actions": "read"}})
    if status != 201:
        raise RuntimeError(f"no installation token ({status})")
    _token.update(value=token["token"], expires=calendar.timegm(time.strptime(token["expires_at"], "%Y-%m-%dT%H:%M:%SZ")))
    return _token["value"]


def app_jwt(app_id, private_key, now=None):
    """The app's JWT, RS256, valid nine minutes, backdated a minute for clock skew."""
    import jwt  # PyJWT, with cryptography for RS256: bridge/build.sh packages both

    now = int(now or time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, private_key, algorithm="RS256")


# Settings, SSM and AWS clients, kept between calls of a warm function.

_clients, _parameters = {}, {}


def aws(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


# In Lambda, make the first call's clients during init, which runs at full speed.
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    aws("ssm"), aws("lambda")


def parameters(*names):
    """SSM values by name, None for a missing or unset one. Kept for five
    minutes, so a warm function answers without asking SSM again."""
    now = time.time()
    stale = [name for name in names if name not in _parameters or _parameters[name][1] < now]
    for start in range(0, len(stale), 10):  # GetParameters takes ten names at a time
        reply = aws("ssm").get_parameters(Names=[SSM_PREFIX + name for name in stale[start:start + 10]],
                                          WithDecryption=True)
        for parameter in reply["Parameters"]:
            value = parameter["Value"]
            _parameters[parameter["Name"].removeprefix(SSM_PREFIX)] = (None if value == "unset" else value,
                                                                       now + CACHE_SECONDS)
    return {name: _parameters.get(name, (None, 0))[0] for name in names}


def http(status, body=None):
    response = {"statusCode": status, "body": ""}
    if body is not None:
        response.update(headers={"Content-Type": "application/json"}, body=json.dumps(body))
    return response


def log(message):
    print(message)
