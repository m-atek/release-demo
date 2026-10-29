"""Demo 2: start the drift check after a change made to the network stack outside Terraform.

EventBridge hands this Lambda function each security group rule change and
each bucket policy or public access block change in eu-west-1, as CloudTrail
records them (infra/demos/2-terraform/drift_trigger.tf). For a change to the
network stack, on a security group tagged Demo = 2-terraform or on a store
exports bucket, it starts the Jenkins job network-drift with TRIGGER=event,
the call's name and time, the resource's id, and whether the call came from
the AWS console. Calls by the stack's own Terraform roles start nothing: the
pull requests, reset.py terraform and stack.py up all apply as the apply
role, and the plan role only reads.

Several changes within a minute start one run. Jenkins merges queued builds
only when their parameters match, so this function decides itself: a change
is in the run still waiting in the queue, or in a run that started after the
change was made. Otherwise it queues one, which waits a quiet period of 60
seconds for more. Two changes at the same moment can both queue one; then the
later queue item gives way to the earlier.

When the tools host isn't running, it logs that and starts nothing: the
07:00 run finds the change.

Standard library and boto3 only, both in Lambda's Python runtime, so the
package is this file. It reaches Jenkins as the Slack handler does: as
release-bot, through Cloudflare Access with the Slack handler's service
token, both read from SSM when it runs and never logged. The environment
(drift_trigger.tf) sets JENKINS_URL, IGNORED_ROLES, DEMO_TAG and BUCKET_PREFIX.
"""

import base64
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

SSM_PREFIX = "/release-demo/"
CACHE_SECONDS = 5 * 60
JOB = "network-drift"
QUIET_SECONDS = 60
TOOLS_HOST = "release-demo-tools-host"
# Cloudflare turns away Python's default User-Agent, so every call names itself.
USER_AGENT = "release-demo-drift-trigger/1.0 (+https://github.com/m-atek/release-demo)"
JENKINS_PARAMETERS = ["cloudflare/slack-handler-client-id", "cloudflare/slack-handler-client-secret",
                      "jenkins/release-bot-api-token"]

# The calls the EventBridge rule passes on: what changes a security group's
# rules, and what changes who may reach a bucket.
GROUP_CALLS = {"AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress", "RevokeSecurityGroupIngress",
               "RevokeSecurityGroupEgress", "ModifySecurityGroupRules"}
BUCKET_CALLS = {"PutBucketPolicy", "DeleteBucketPolicy", "PutBucketPublicAccessBlock",
                "DeleteBucketPublicAccessBlock"}


def handler(event, context):
    """Never raises: the log says what happened, and the 07:00 run is the safety net."""
    try:
        change = what_changed(event.get("detail") or {})
        if change:
            start(change)
    except Exception as error:  # the log has the detail
        log(f"failed: {error.__class__.__name__}: {error}")


# Which changes count

def what_changed(detail):
    """The change, as network-drift's parameters take it, from a CloudTrail record.
    None when the drift check has nothing to look at; the log says why."""
    name = detail.get("eventName") or "an event"
    if name not in GROUP_CALLS | BUCKET_CALLS:
        return skip(f"{name}: not a call this function watches")
    if detail.get("errorCode"):
        return skip(f"{name} failed ({detail['errorCode']}): nothing changed")
    role = caller_role(detail.get("userIdentity") or {})
    if role in ignored_roles():
        return skip(f"{name} by {role}: the network stack's own Terraform role")
    if name in GROUP_CALLS:
        resource = find(detail.get("requestParameters"), ("groupId", "GroupId"))
        ours = bool(resource) and stack_group(resource)
    else:
        resource = (detail.get("requestParameters") or {}).get("bucketName") or ""
        ours = resource.startswith(bucket_prefix())
    if not ours:
        # Not named: another demo's bucket name can hold the account id.
        return skip(f"{name}: not on the network stack")
    return {"name": name, "time": detail.get("eventTime", ""), "resource": resource,
            "console": from_console(detail)}


def caller_role(identity):
    """The IAM role behind an assumed-role session, such as release-demo-deploy-network-apply."""
    issuer = (identity.get("sessionContext") or {}).get("sessionIssuer") or {}
    if issuer.get("type") == "Role" and issuer.get("userName"):
        return issuer["userName"]
    match = re.match(r"arn:aws:sts::\d+:assumed-role/([^/]+)/", identity.get("arn") or "")
    return match.group(1) if match else None


def find(value, keys):
    """The first text under one of keys in a record's parameters, outermost first: the
    group a rule call changes, not a group one of its rules refers to.
    ModifySecurityGroupRules nests it: {"ModifySecurityGroupRulesRequest": {"GroupId": ...}}."""
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            found = find(item, keys)
            if found:
                return found
    return None


def stack_group(group_id):
    """Is the security group one of the network stack's? Terraform tags each of them Demo = 2-terraform."""
    try:
        groups = aws("ec2").describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
    except ClientError:  # gone already, or not a group id
        return False
    return any({"Key": "Demo", "Value": demo_tag()} in group.get("Tags", []) for group in groups)


def from_console(detail):
    """CloudTrail marks a call made with an AWS console session."""
    flag = detail.get("sessionCredentialFromConsole") or (detail.get("userIdentity") or {}).get(
        "sessionCredentialFromConsole")
    return str(flag).lower() == "true"


def skip(reason):
    log(f"ignored: {reason}")
    return None


# Starting network-drift, once per minute of changes

def start(change):
    what = f"{change['name']} on {change['resource']} at {change['time']}"
    state = tools_host_state()
    if state != "running":
        log(f"{what}: the tools host is {state or 'not there'}, so nothing started. The 07:00 run will find it.")
        return
    secrets = parameters(*JENKINS_PARAMETERS)
    missing = [name for name in JENKINS_PARAMETERS if not secrets[name]]
    if missing:
        log(f"{what}: not set in SSM: {', '.join(missing)}. Nothing started.")
        return

    covered = covering_run(change)
    if covered:
        log(f"{what}: in {covered}")
        return
    status, item = queue(change)
    if status != 201:
        log(f"{what}: {problem(status, item)} Nothing started.")
        return
    earlier = earlier_item(item)
    if earlier:
        gave_way = cancel(item)
        log(f"{what}: in queue item {earlier}, queued a moment before"
            + ("" if gave_way else f"; queue item {item} couldn't give way, so it runs too"))
        return
    log(f"{what}: {JOB} queued as item {item}, starting in {QUIET_SECONDS} s")


def covering_run(change):
    """The run this change is in already, in words, or None. One still waiting in the
    queue plans after it; so does one that started after the change was made."""
    status, _, body = jenkins("GET", f"/job/{JOB}/api/json?"
                              + urllib.parse.urlencode({"tree": "inQueue,lastBuild[number,timestamp]"}))
    if status != 200:
        return None  # queueing says what is wrong
    job = json.loads(body)
    if job.get("inQueue"):
        return f"the {JOB} run waiting in the queue"
    last, made = job.get("lastBuild") or {}, epoch(change["time"])
    if last and made is not None and last.get("timestamp", 0) / 1000 >= made:
        return f"{JOB} #{last['number']}, which started after it"
    return None


def queue(change):
    """network-drift with the change as its parameters, after the quiet period.
    Returns (HTTP status, queue item id), or (0, why) when nothing answered."""
    params = {"TRIGGER": "event", "EVENT_NAME": change["name"], "EVENT_TIME": change["time"],
              "RESOURCE_ID": change["resource"], "FROM_CONSOLE": "true" if change["console"] else "false"}
    status, headers, body = jenkins("POST", f"/job/{JOB}/buildWithParameters?delay={QUIET_SECONDS}sec", params)
    if status != 201:
        return status, body.decode(errors="replace")[:200] if status == 0 else ""
    match = re.search(r"/queue/item/(\d+)", headers.get("Location") or "")
    return status, int(match.group(1)) if match else None


def earlier_item(item):
    """The oldest network-drift queue item queued before this one, if any."""
    if item is None:
        return None
    status, _, body = jenkins("GET", "/queue/api/json?" + urllib.parse.urlencode({"tree": "items[id,task[name]]"}))
    if status != 200:
        return None
    ids = [entry["id"] for entry in json.loads(body).get("items", [])
           if (entry.get("task") or {}).get("name") == JOB and entry.get("id", item) < item]
    return min(ids) if ids else None


def cancel(item):
    status, _, _ = jenkins("POST", f"/queue/cancelItem?id={item}")
    return 200 <= status < 400


def problem(status, why=""):
    if status == 0:
        return f"Jenkins didn't answer ({why})."
    if status in (301, 302, 303, 307, 401, 403):
        return (f"Jenkins or Cloudflare Access refused release-bot ({status}): check jenkins/release-bot-api-token "
                "and cloudflare/slack-handler-client-id in SSM.")
    return f"Jenkins answered {status}."


def epoch(text):
    """CloudTrail's eventTime, such as 2026-10-29T16:54:24Z, in seconds since the epoch."""
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)).timestamp()


def tools_host_state():
    reply = aws("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [TOOLS_HOST]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    states = [i["State"]["Name"] for r in reply["Reservations"] for i in r["Instances"]]
    return "running" if "running" in states else (states[0] if states else None)


# Jenkins, as release-bot through Cloudflare Access

def jenkins(method, path, form=None):
    """Returns (HTTP status, headers, body); status 0 when nothing answered. A 401 reads
    release-bot's token from SSM again, once: the host makes a new token when the
    stored one stops working, and a warm function may still hold the old one."""
    for attempt in (1, 2):
        status, headers, body = call(parameters(*JENKINS_PARAMETERS), method, path, form)
        if status != 401 or attempt == 2:
            return status, headers, body
        forget("jenkins/release-bot-api-token")


def call(secrets, method, path, form):
    bot = base64.b64encode(f"release-bot:{secrets['jenkins/release-bot-api-token']}".encode()).decode()
    headers = {"CF-Access-Client-Id": secrets["cloudflare/slack-handler-client-id"],
               "CF-Access-Client-Secret": secrets["cloudflare/slack-handler-client-secret"],
               "Authorization": f"Basic {bot}", "User-Agent": USER_AGENT}
    data = None
    if method == "POST":  # with a length, even when empty, as proxies want
        data = urllib.parse.urlencode(form or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(os.environ["JENKINS_URL"].rstrip("/") + path, method=method, data=data,
                                     headers=headers)
    try:
        with OPENER.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers or {}, b""
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return 0, {}, str(getattr(error, "reason", error)).encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Cloudflare Access answers a refused token with a redirect to its login
    page. Followed, that would look like success."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


# Settings, SSM and AWS clients, kept between calls of a warm function

_clients, _parameters = {}, {}


def aws(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def ignored_roles():
    return {name.strip() for name in os.environ.get("IGNORED_ROLES", "").split(",") if name.strip()}


def demo_tag():
    return os.environ.get("DEMO_TAG", "2-terraform")


def bucket_prefix():
    return os.environ.get("BUCKET_PREFIX", "release-demo-store-exports-")


def parameters(*names):
    """SSM values by name, None for a missing or unset one, kept for five minutes."""
    now = time.time()
    stale = [name for name in names if name not in _parameters or _parameters[name][1] < now]
    if stale:
        reply = aws("ssm").get_parameters(Names=[SSM_PREFIX + name for name in stale], WithDecryption=True)
        for parameter in reply["Parameters"]:
            value = parameter["Value"]
            _parameters[parameter["Name"].removeprefix(SSM_PREFIX)] = (None if value == "unset" else value,
                                                                       now + CACHE_SECONDS)
    return {name: _parameters.get(name, (None, 0))[0] for name in names}


def forget(name):
    _parameters.pop(name, None)


def log(message):
    print(message)
