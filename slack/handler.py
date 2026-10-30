"""The Slack handler: /release, /rollback, /agent and /ask, from Slack to Jenkins, and /flag, to AWS AppConfig.

Slack posts each slash command to this function's URL and wants an answer
within 3 seconds. So the first call only checks the request (Slack's
signature, no older than five minutes), the person (a releaser) and the
command (against services.yaml), answers at once, and invokes this function
again, asynchronously, for the slow part: is the tools host running, post the
status message the pipeline keeps up to date, and start the Jenkins job as
release-bot through Cloudflare Access.

A release may run on another CI instead: ci/<service> in SSM, read afresh
for every /release, names it. jenkins is the default and the value whenever
another CI's demo is down; with gitlab-ci (demo 17) the handler starts the
GitLab pipeline with the project's trigger token, and with azure-pipelines
(demo 22) it queues the Azure Pipelines run as Release Bot, which signs in
with no stored secret: AWS signs a short-lived token for this function's
role, and Entra trusts it for Release Bot. /rollback always goes to Jenkins.

/flag <service> <flag> on|off (demo 21) switches one feature flag in
production: a new version of the service's AppConfig flags profile with that
flag changed, deployed with the strategy services.yaml names. It answers at
once and needs neither Jenkins nor the tools host; the event bridge records
the switch as a change (bridge/appconfig.py).

Standard library and boto3 only, both in Lambda's Python runtime, so the
package is this file plus config.json, which Terraform writes from
services.yaml. The environment (infra/slack_handler.tf) sets RELEASERS, the
people.yaml names with releaser: true, whose Slack member ids are in SSM;
TOOLS_HOST_ID; and JENKINS_URL. Secrets come from SSM and are never logged.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

SSM_PREFIX = "/release-demo/"
MAX_AGE_SECONDS = 5 * 60
CACHE_SECONDS = 5 * 60
# Cloudflare turns away Python's default User-Agent, so every call names itself.
USER_AGENT = "release-demo-slack-handler/1.0 (+https://github.com/m-atek/release-demo)"

NOT_A_RELEASER = "You can't start releases here."
NOT_IN_THE_TEAM = "You can't ask here."
ASK_USAGE = "Usage: /ask <question>, 3 to 300 characters, for example /ask How do I roll back orders-service?"
ASK_JOB = "runbook-assistant-ask"
STOPPED = "The release server is stopped. Start it with scripts/tools.sh up."
STARTING = "The release server is starting. Try again when scripts/tools.sh up says Ready."
JENKINS_PARAMETERS = ["slack/bot-token", "cloudflare/slack-handler-client-id",
                      "cloudflare/slack-handler-client-secret", "jenkins/release-bot-api-token"]
# Demo 17: GitLab CI starts the release on the runner on the tools host.
GITLAB_API = "https://gitlab.com/api/v4"
GITLAB_PARAMETERS = ["slack/bot-token", "gitlab/project-id", "gitlab/trigger-token"]
# The Jenkins job's parameters, as .gitlab-ci.yml's inputs.
GITLAB_INPUTS = {"SERVICE": "service", "JIRA_KEY": "jira_key", "OPTION": "option", "REQUESTED_BY": "requested_by",
                 "SLACK_CHANNEL": "slack_channel", "SLACK_TS": "slack_ts", "REQUESTED_AT": "requested_at"}
# Demo 22: Azure Pipelines, queued as Release Bot.
AZURE_PROJECT = "release-demo"
AZURE_PARAMETERS = ["slack/bot-token", "azure-devops/organization", "azure-devops/pipeline-id",
                    "azure-devops/tenant-id", "azure-devops/release-bot-client-id"]
# The Jenkins job's parameters, as azure-pipelines.yml's.
AZURE_INPUTS = {"SERVICE": "service", "JIRA_KEY": "jiraKey", "OPTION": "option", "REQUESTED_BY": "requestedBy",
                "SLACK_CHANNEL": "slackChannel", "SLACK_TS": "slackTs", "REQUESTED_AT": "requestedAt"}
AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"  # Azure DevOps, as Entra names it
TOKEN_EXCHANGE = "api://AzureADTokenExchange"
CIS = ("jenkins", "gitlab-ci", "azure-pipelines")


class Refusal(Exception):
    """A reply for the person who typed the command, and nobody else."""


def handler(event, context):
    if "work" in event:  # the asynchronous call: only this function's role can send it
        run(event["work"])
        return None
    return from_slack(event, context)


# The first call: check, answer, hand over.

def from_slack(event, context):
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    headers = {name.lower(): value for name, value in (event.get("headers") or {}).items()}
    form = {key: values[0] for key, values in urllib.parse.parse_qs(body.decode(errors="replace"),
                                                                    keep_blank_values=True).items()}
    if form.get("ssl_check") == "1":  # Slack checking the certificate: nothing to do
        return http(200)

    member_ids = [f"slack/member-id/{name}" for name in releasers()]
    asker_ids = [f"slack/member-id/{name}" for name in askers()]
    values = parameters("slack/signing-secret", *dict.fromkeys(member_ids + asker_ids))
    secret = values["slack/signing-secret"]
    if not secret:
        log("refused: slack/signing-secret isn't set in SSM")
        return http(500)
    problem = verify(secret, headers, body)
    if problem:
        log(f"refused: {problem}")
        return http(401)

    user, command = form.get("user_id", ""), form.get("command", "")
    if command == "/ask":
        return ask(form, user, {values[name] for name in asker_ids if values[name]}, context)
    if user not in {values[name] for name in member_ids if values[name]}:
        log(f"{user} {command}: not a releaser")
        return reply(NOT_A_RELEASER)
    try:
        job, params = parse(command, form.get("text", ""), config()["services"])
    except Refusal as refusal:
        log(f"{user} {command}: {refusal}")
        return reply(str(refusal))

    if job == "flag":
        # Demo 21: who asked, by the name people.yaml gives them, for the deployment and the change.
        name = next((n for n in releasers() if values[f"slack/member-id/{n}"] == user), "")
        params["PERSON"] = config().get("people", {}).get(name, name)
    # REQUESTED_AT, the verified time of the command, is where the run's total
    # time starts: the video's clock starts when Sam presses Enter.
    work = {"job": job,
            "params": {**params, "REQUESTED_BY": user, "REQUESTED_AT": headers["x-slack-request-timestamp"]},
            "channel": form.get("channel_id", ""), "response_url": form.get("response_url", "")}
    aws("lambda").invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                         Payload=json.dumps({"work": work}).encode())
    log(f"{user} {command} {form.get('text', '')}: handed over")
    if job == "flag":
        return reply(f"Switching {params['FLAG']} {params['STATE']} in {params['SERVICE']} production.")
    return http(200)


def ask(form, user, team, context):
    """Demo 23's /ask: anyone in people.yaml with Slack may ask the runbook assistant,
    not only releasers. The question goes to Jenkins as a parameter, never into a script."""
    text = form.get("text", "").strip()
    if user not in team:
        log(f"{user} /ask: not in people.yaml")
        return reply(NOT_IN_THE_TEAM)
    if not 3 <= len(text) <= 300:
        return reply(ASK_USAGE)
    work = {"job": ASK_JOB, "params": {"QUESTION": text, "ASKED_BY": user},
            "channel": form.get("channel_id", ""), "response_url": form.get("response_url", "")}
    aws("lambda").invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                         Payload=json.dumps({"work": work}).encode())
    log(f"{user} /ask: handed over")
    return http(200)


def verify(secret, headers, body, now=None):
    """None when the request is Slack's and at most five minutes old, else why not.
    Slack signs "v0:<timestamp>:<body>" with the app's signing secret."""
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not timestamp.isdigit() or not signature:
        return "no Slack signature"
    if abs((now or time.time()) - int(timestamp)) > MAX_AGE_SECONDS:
        return "older than five minutes"
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
                                hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return "bad signature"
    return None


def parse(command, text, services):
    """The Jenkins job and its parameters for /release or /rollback, or a Refusal
    that says what to type instead."""
    words = text.split()
    if command == "/release":
        if len(words) not in (2, 3):
            raise Refusal("Usage: /release <service> <Jira key> [option], for example /release orders-service ORD-12")
        name, key = words[0].lower(), words[1].upper()
        option = words[2].lower() if len(words) == 3 else ""
        settings = known(name, services)
        project = settings["jira_project"]
        if not re.fullmatch(rf"{re.escape(project)}-[1-9][0-9]*", key):
            raise Refusal(f"{words[1]} isn't a Jira key for {name}: it takes {project}-<number>, such as {project}-12.")
        if option and option not in settings["options"]:
            allowed = ", ".join(settings["options"]) or "none"
            raise Refusal(f"{name} has no option {words[2]}. Its options: {allowed}.")
        return "release", {"SERVICE": name, "JIRA_KEY": key, "OPTION": option}
    if command == "/rollback":
        if len(words) != 1:
            raise Refusal("Usage: /rollback <service>, for example /rollback orders-service")
        name = words[0].lower()
        known(name, services)
        return "rollback", {"SERVICE": name}
    if command == "/agent":
        return parse_agent(words, text, services)
    if command == "/flag":
        return "flag", flag_request(words, services)
    raise Refusal(f"This app knows /release, /rollback, /agent and /flag, not {command or 'that'}.")


# Video 16: /agent <service> <Jira key> <note> hands a Jira issue to the coding agent.
NOTE_LIMIT = 300


def parse_agent(words, text, services):
    if len(words) < 3:
        raise Refusal("Usage: /agent <service> <Jira key> <note>, for example "
                      "/agent orders-service ORD-91 Please ship this today")
    name, key = words[0].lower(), words[1].upper()
    settings = known(name, services)
    if not settings.get("coding_agent"):
        raise Refusal(f"The coding agent doesn't work on {name}.")
    project = settings["jira_project"]
    if not re.fullmatch(rf"{re.escape(project)}-[1-9][0-9]*", key):
        raise Refusal(f"{words[1]} isn't a Jira key for {name}: it takes {project}-<number>, such as {project}-12.")
    note = text.strip().split(None, 2)[2].strip()
    if len(note) > NOTE_LIMIT:
        raise Refusal(f"Keep the note to {NOTE_LIMIT} characters; this one has {len(note)}.")
    return "agent-task", {"SERVICE": name, "JIRA_KEY": key, "NOTE": note}


def flag_request(words, services):
    """/flag <service> <flag> on|off, in production only (demo 21)."""
    usage = "Usage: /flag <service> <flag> on|off, for example /flag orders-service delivery_estimate_v2 on"
    if len(words) < 3:
        raise Refusal(usage)
    name, flag, state = words[0].lower(), words[1], words[2].lower()
    settings = known(name, services)
    flags = settings.get("flags") or []
    if not flags:
        raise Refusal(f"{name} has no feature flags.")
    if flag not in flags:
        raise Refusal(f"{name} has no flag {flag}. Its flags: {', '.join(flags)}.")
    if state not in ("on", "off"):
        raise Refusal(usage)
    if len(words) > 3:
        raise Refusal(f"/flag switches flags in production only: /flag {name} {flag} {state}")
    return {"SERVICE": name, "FLAG": flag, "STATE": state}


def known(name, services):
    if name not in services:
        raise Refusal(f"There's no service {name}. Services: {', '.join(sorted(services))}.")
    return services[name]


# The asynchronous call: the tools host, the status message, the Jenkins job.

def run(work):
    """Never raises: Lambda would retry the event, and a retry could start a second run."""
    if work["job"] == "flag":
        switch_flag(work)
        return
    try:
        state = tools_host_state()
        if state != "running":
            log(f"tools host is {state}")
            say(work["response_url"], STARTING if state == "pending" else STOPPED)
            return
        ci = ci_for(work["params"]["SERVICE"]) if work["job"] == "release" else "jenkins"
        if ci == "gitlab-ci":
            run_on_gitlab(work)
            return
        if ci == "azure-pipelines":
            run_on_azure(work)
            return
        secrets = parameters(*JENKINS_PARAMETERS)
        missing = [name for name in JENKINS_PARAMETERS if not secrets[name]]
        if missing:
            log(f"not set in SSM: {', '.join(missing)}")
            say(work["response_url"], f"The release server isn't set up yet: {', '.join(missing)} isn't in SSM.")
            return

        token = secrets["slack/bot-token"]
        posted = slack(token, "chat.postMessage", channel=work["channel"], text=status_message(work),
                       unfurl_links=False)
        if not posted.get("ok"):
            log(f"chat.postMessage: {posted.get('error')}")
            say(work["response_url"], f"I can't post in this channel ({posted.get('error')}). "
                                      "Invite me with /invite @Release Bot, or use #releases.")
            return

        params = {**work["params"], "SLACK_CHANNEL": posted["channel"], "SLACK_TS": posted["ts"]}
        status, where = start_job(secrets, work["job"], params)
        if status == 401:  # the host makes a new token when the stored one stops working
            forget("jenkins/release-bot-api-token")
            status, where = start_job(parameters(*JENKINS_PARAMETERS), work["job"], params)
        if status == 201:
            log(f"{work['job']} queued: {where}")
            return
        log(f"Jenkins answered {status or where}")
        slack(token, "chat.update", channel=posted["channel"], ts=posted["ts"],
              text=status_message(work, f":x:  Not started: {jenkins_problem(status)}"))
    except Exception as error:  # the log has the detail; Slack gets no internals
        log(f"failed: {error.__class__.__name__}: {error}")
        say(work.get("response_url", ""), f"The release didn't start ({error.__class__.__name__}). "
                                          "The Slack handler's log in CloudWatch says why.")


def switch_flag(work):
    """A new version of the flags profile with one flag changed, deployed to
    production. Its description names who asked, and its tags what changed,
    for the event bridge's record. Never raises, as run() doesn't."""
    p = work["params"]
    try:
        ids = json.loads(parameters(f"appconfig/{p['SERVICE']}")[f"appconfig/{p['SERVICE']}"] or "{}")
        if not ids:
            say(work["response_url"], f"{p['SERVICE']}'s flags aren't set up: appconfig/{p['SERVICE']} isn't in SSM.")
            return
        client = aws("appconfig")
        versions = client.list_hosted_configuration_versions(ApplicationId=ids["application"],
                                                             ConfigurationProfileId=ids["profile"])["Items"]
        latest = max(v["VersionNumber"] for v in versions)
        content = json.loads(client.get_hosted_configuration_version(
            ApplicationId=ids["application"], ConfigurationProfileId=ids["profile"], VersionNumber=latest)["Content"].read())
        content.setdefault("values", {})[p["FLAG"]] = {**content["values"].get(p["FLAG"], {}), "enabled": p["STATE"] == "on"}
        made = client.create_hosted_configuration_version(
            ApplicationId=ids["application"], ConfigurationProfileId=ids["profile"], Content=json.dumps(content).encode(),
            ContentType="application/json", LatestVersionNumber=latest,
            Description=f"{p['FLAG']} {p['STATE']}, by {p.get('PERSON') or p['REQUESTED_BY']} in Slack")
        deployment = client.start_deployment(
            ApplicationId=ids["application"], EnvironmentId=ids["environments"]["production"],
            DeploymentStrategyId=ids["strategy"], ConfigurationProfileId=ids["profile"],
            ConfigurationVersion=str(made["VersionNumber"]), Description=f"{p.get('PERSON') or 'Slack'} in Slack",
            Tags={"requested_by": p["REQUESTED_BY"], "flag": p["FLAG"], "state": p["STATE"]})
        log(f"flag {p['FLAG']} {p['STATE']}: version {made['VersionNumber']}, deployment {deployment['DeploymentNumber']}")
    except Exception as error:
        busy = error.__class__.__name__ == "ConflictException"
        log(f"flag {p['FLAG']} {p['STATE']}: {error.__class__.__name__}: {error}")
        say(work.get("response_url", ""), (
            f"{p['FLAG']} can't switch now: an AWS AppConfig deployment still runs in {p['SERVICE']} production. "
            "It ends when its 10 minutes of bake time are over.") if busy else (
            f"{p['FLAG']} didn't switch ({error.__class__.__name__}). The Slack handler's log in CloudWatch says why."))


def tools_host_state():
    reply = aws("ec2").describe_instances(InstanceIds=[os.environ["TOOLS_HOST_ID"]])
    return reply["Reservations"][0]["Instances"][0]["State"]["Name"]


# Demo 17: the same release on GitLab CI.

def ci_for(service):
    """Which CI /release starts for a service: ci/<service> in SSM, read afresh
    every time, so a switch takes effect at once. jenkins when it is unset."""
    reply = aws("ssm").get_parameters(Names=[f"{SSM_PREFIX}ci/{service}"], WithDecryption=True)
    value = next((p["Value"] for p in reply["Parameters"]), "")
    return value if value in CIS else "jenkins"


def run_on_gitlab(work):
    """The status message, then the pipeline, with the trigger token: the
    Jenkins job's parameters as its inputs, from main."""
    secrets = parameters(*GITLAB_PARAMETERS)
    missing = [name for name in GITLAB_PARAMETERS if not secrets[name]]
    if missing:
        log(f"not set in SSM: {', '.join(missing)}")
        say(work["response_url"], f"GitLab CI isn't set up yet: {', '.join(missing)} isn't in SSM.")
        return
    token = secrets["slack/bot-token"]
    starting = ":hourglass_flowing_sand:  Starting on GitLab CI"
    posted = slack(token, "chat.postMessage", channel=work["channel"], text=status_message(work, starting),
                   unfurl_links=False)
    if not posted.get("ok"):
        log(f"chat.postMessage: {posted.get('error')}")
        say(work["response_url"], f"I can't post in this channel ({posted.get('error')}). "
                                  "Invite me with /invite @Release Bot, or use #releases.")
        return
    params = {**work["params"], "SLACK_CHANNEL": posted["channel"], "SLACK_TS": posted["ts"]}
    status, where = trigger_gitlab(secrets, params)
    if status == 201:
        log(f"GitLab pipeline started: {where}")
        return
    log(f"GitLab answered {status or where}")
    slack(token, "chat.update", channel=posted["channel"], ts=posted["ts"],
          text=status_message(work, f":x:  Not started: GitLab answered {status or 'nothing'}."))


def trigger_gitlab(secrets, params):
    """POST /projects/:id/trigger/pipeline. Returns (HTTP status, the pipeline's
    page or error); status 0 when nothing answered."""
    form = {"token": secrets["gitlab/trigger-token"], "ref": "main",
            **{f"inputs[{GITLAB_INPUTS[k]}]": v for k, v in params.items() if k in GITLAB_INPUTS}}
    request = urllib.request.Request(
        f"{GITLAB_API}/projects/{secrets['gitlab/project-id']}/trigger/pipeline", method="POST",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with GITLAB_OPENER.open(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}").get("web_url", "")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return 0, str(getattr(error, "reason", error))


GITLAB_OPENER = urllib.request.build_opener()


# Demo 22: the same release on Azure Pipelines.

def run_on_azure(work):
    """The status message, then the run, queued with the Runs API as Release Bot:
    the Jenkins job's parameters as the pipeline's, from main."""
    secrets = parameters(*AZURE_PARAMETERS)
    missing = [name for name in AZURE_PARAMETERS if not secrets[name]]
    if missing:
        log(f"not set in SSM: {', '.join(missing)}")
        say(work["response_url"], f"Azure Pipelines isn't set up yet: {', '.join(missing)} isn't in SSM.")
        return
    token = secrets["slack/bot-token"]
    starting = ":hourglass_flowing_sand:  Starting on Azure Pipelines"
    posted = slack(token, "chat.postMessage", channel=work["channel"], text=status_message(work, starting),
                   unfurl_links=False)
    if not posted.get("ok"):
        log(f"chat.postMessage: {posted.get('error')}")
        say(work["response_url"], f"I can't post in this channel ({posted.get('error')}). "
                                  "Invite me with /invite @Release Bot, or use #releases.")
        return
    params = {**work["params"], "SLACK_CHANNEL": posted["channel"], "SLACK_TS": posted["ts"]}
    status, bot = release_bot_token(secrets)
    if bot is None:
        problem = f"Release Bot couldn't sign in to Azure DevOps (Entra answered {status or 'nothing'})."
    else:
        status, where = queue_azure_run(secrets, bot, params)
        if status == 200:
            log(f"Azure Pipelines run queued: {where}")
            return
        problem = f"Azure DevOps answered {status or 'nothing'}."
    log(problem)
    slack(token, "chat.update", channel=posted["channel"], ts=posted["ts"],
          text=status_message(work, f":x:  Not started: {problem}"))


def web_identity_token():
    """A five-minute token AWS signs for this function's role, for Entra:
    sts:GetWebIdentityToken, AWS's outbound identity federation."""
    sts = aws("sts")
    if hasattr(sts, "get_web_identity_token"):
        return sts.get_web_identity_token(Audience=[TOKEN_EXCHANGE], SigningAlgorithm="RS256",
                                          DurationSeconds=300)["WebIdentityToken"]
    # A Lambda runtime whose botocore predates the call: the same request, signed here.
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = os.environ.get("AWS_REGION", "eu-west-1")
    body = urllib.parse.urlencode({"Action": "GetWebIdentityToken", "Version": "2011-06-15",
                                   "Audience.member.1": TOKEN_EXCHANGE, "SigningAlgorithm": "RS256",
                                   "DurationSeconds": "300"})
    request = AWSRequest(method="POST", url=f"https://sts.{region}.amazonaws.com/", data=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    SigV4Auth(boto3.Session().get_credentials().get_frozen_credentials(), "sts", region).add_auth(request)
    sent = urllib.request.Request(request.url, data=body.encode(), method="POST", headers=dict(request.headers))
    with urllib.request.urlopen(sent, timeout=10) as response:
        found = re.search(r"<WebIdentityToken>([^<]+)</WebIdentityToken>", response.read().decode())
    return found.group(1)


def release_bot_token(secrets):
    """(status, Azure DevOps access token as Release Bot), or (status, None):
    Entra exchanges AWS's token through Release Bot's federated credential."""
    form = {"client_id": secrets["azure-devops/release-bot-client-id"], "scope": AZURE_DEVOPS_SCOPE,
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": web_identity_token()}
    request = urllib.request.Request(
        f"https://login.microsoftonline.com/{secrets['azure-devops/tenant-id']}/oauth2/v2.0/token", method="POST",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with AZURE_OPENER.open(request, timeout=10) as response:
            return response.status, json.loads(response.read())["access_token"]
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, None


def queue_azure_run(secrets, token, params):
    """The Runs API: (HTTP status, the run's page or error); status 0 when nothing answered."""
    organization, pipeline = secrets["azure-devops/organization"], secrets["azure-devops/pipeline-id"]
    body = {"resources": {"repositories": {"self": {"refName": "refs/heads/main"}}},
            "templateParameters": {AZURE_INPUTS[k]: v for k, v in params.items() if k in AZURE_INPUTS}}
    request = urllib.request.Request(
        f"https://dev.azure.com/{organization}/{AZURE_PROJECT}/_apis/pipelines/{pipeline}/runs?api-version=7.1",
        method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with AZURE_OPENER.open(request, timeout=10) as response:
            reply = json.loads(response.read() or b"{}")
            return response.status, reply.get("_links", {}).get("web", {}).get("href", "")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return 0, str(getattr(error, "reason", error))


AZURE_OPENER = urllib.request.build_opener()


def status_message(work, line=":hourglass_flowing_sand:  Starting on Jenkins"):
    """The first version of the message the pipeline then rewrites at every stage."""
    p = work["params"]
    if work["job"] == ASK_JOB:
        return f"Question from <@{p['ASKED_BY']}>: {p['QUESTION']}\n\n{line}"
    if work["job"] == "release":
        option = f" · {p['OPTION']}" if p.get("OPTION") else ""
        head = f"*Release {p['SERVICE']}* · {p['JIRA_KEY']}{option} · requested by <@{p['REQUESTED_BY']}>"
    elif work["job"] == "agent-task":
        # agent-task adds the issue's summary once it has read it from Jira.
        head = f"*Coding agent on {p['JIRA_KEY']}* · asked by <@{p['REQUESTED_BY']}>\n> {p['NOTE']}"
    else:
        head = f"*Rollback {p['SERVICE']}* · production · requested by <@{p['REQUESTED_BY']}>"
    return f"{head}\n\n{line}"


def start_job(secrets, job, params):
    """buildWithParameters as release-bot, through Cloudflare Access with the
    handler's service token. Returns (HTTP status, queue item URL or error);
    status 0 when nothing answered. delay=0sec skips Jenkins' quiet period."""
    url = f"{os.environ['JENKINS_URL'].rstrip('/')}/job/{job}/buildWithParameters?delay=0sec"
    bot = base64.b64encode(f"release-bot:{secrets['jenkins/release-bot-api-token']}".encode()).decode()
    request = urllib.request.Request(url, method="POST", data=urllib.parse.urlencode(params).encode(), headers={
        "CF-Access-Client-Id": secrets["cloudflare/slack-handler-client-id"],
        "CF-Access-Client-Secret": secrets["cloudflare/slack-handler-client-secret"],
        "Authorization": f"Basic {bot}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    })
    try:
        with OPENER.open(request, timeout=10) as response:
            return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        return 0, str(getattr(error, "reason", error))


def jenkins_problem(status):
    if status in (0, 502, 503, 504, 521, 522, 523, 524, 530):
        return (f"Jenkins doesn't answer{f' ({status})' if status else ''}. If the release server just started, "
                "try again when scripts/tools.sh up says Ready.")
    if status in (301, 302, 303, 307, 401, 403):
        return (f"Jenkins or Cloudflare Access refused release-bot ({status}). Check its tokens in SSM: "
                "jenkins/release-bot-api-token and cloudflare/slack-handler-client-id.")
    return f"Jenkins answered {status}."


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Cloudflare Access answers a refused token with a redirect to its login
    page. Followed, that would look like success."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def say(response_url, text):
    """A private reply to whoever typed the command, through Slack's response_url."""
    if not response_url.startswith("https://hooks.slack.com/"):
        log("no Slack response_url to answer on")
        return
    post_json(response_url, {"response_type": "ephemeral", "text": text})


def slack(token, method, **fields):
    reply = post_json(f"https://slack.com/api/{method}", fields, {"Authorization": f"Bearer {token}"})
    return reply if isinstance(reply, dict) else {"ok": False, "error": "no answer"}


def post_json(url, body, headers=None):
    request = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        log(f"POST {url.split('/')[2]}: {error.__class__.__name__}")
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"ok": raw.strip() == b"ok"}  # response_url answers plain "ok"


# Settings, SSM and AWS clients, kept between calls of a warm function.

_clients, _parameters, _config = {}, {}, {}


def aws(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


# In Lambda, make the first call's clients during init, which runs at full
# speed, so a cold start still answers Slack well within 3 seconds.
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    aws("ssm"), aws("lambda")


def config():
    if not _config:
        _config.update(json.loads((Path(__file__).parent / "config.json").read_text()))
    return _config


def releasers():
    return [name.strip() for name in os.environ.get("RELEASERS", "").split(",") if name.strip()]


def askers():
    """Everyone in people.yaml with Slack (ASKERS, from infra/slack_handler.tf): who may /ask."""
    return [name.strip() for name in os.environ.get("ASKERS", "").split(",") if name.strip()]


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


def forget(name):
    _parameters.pop(name, None)


def http(status, body=None):
    response = {"statusCode": status, "body": ""}
    if body is not None:
        response.update(headers={"Content-Type": "application/json"}, body=json.dumps(body))
    return response


def reply(text):
    """An answer only the person who typed the command sees."""
    return http(200, {"response_type": "ephemeral", "text": text})


def log(message):
    print(message)
