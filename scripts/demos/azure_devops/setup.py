"""Demo 22's Azure DevOps project, with your own az login, the organization's owner.

    python scripts/demos/azure_devops/setup.py --apply --organization <name>    make what is missing
    python scripts/demos/azure_devops/setup.py --check                          what the checks and the video need
    python scripts/demos/azure_devops/setup.py --apply --bridge-test            also the day-one environment and pipeline

Before: the organization (dev.azure.com, signed in with your es.nl account),
the private project release-demo, and in it Pipelines, New pipeline, GitHub,
with the Azure Pipelines GitHub App installed on the repository only; stop at
the review step. And infra/demos/22-azure-devops-approval applied: Release Bot.

--apply makes, where they are missing:
- the agent pool release-demo, with automatic agent updates off (the agent is
  pinned), and its queue in the project;
- the environments staging, with no checks, and production, with no approvals
  and one check: Invoke REST API, "ServiceNow change approved", on the Generic
  service connection "ServiceNow approval bridge" (the bridge's URL, no user
  name or password), POST, the headers Microsoft's docs list plus BuildId,
  completion event Callback, time between evaluations 0, timeout 60 minutes;
- the pipeline release from azure-pipelines.yml on GitHub, the only pipeline
  that may use the environments and the pool;
- Release Bot in the organization (Basic access) and in the project group
  Release bots, which may see the project (View project-level information:
  without it Azure DevOps hides the project from Release Bot, and every call
  into it answers 404), queue the release pipeline and read and cancel its
  runs, and nothing else.
It writes the organization's name and the project's and pipeline's ids to SSM.
Safe to run again: it changes only what is missing.

--check prints what the acceptance checks need: the approvals and checks on
both environments, who may use production, who has its Administrator role
(the role that may bypass a check), the free self-hosted parallel job, the
agent, Release bots' rights, and that the project holds no variable groups,
secure files or secret variables.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from release_bot import AGENT, POOL, PROJECT, SSM_PREFIX, fail, get_parameter, put_parameter, ssm_client

API = "api-version=7.1"
PREVIEW = "api-version=7.1-preview.1"
TASK_CHECK = {"id": "fe1de3ee-a436-41b4-bb20-f6eb4cb879a7", "name": "Task Check"}
APPROVAL_CHECK = "8c6f20a7-a545-4486-9777-f762fafe0d4d"
INVOKE_REST_API = {"id": "9c3e8943-130d-4c78-ac63-8af81df62dfb", "name": "InvokeRESTAPI", "version": "1.220.0"}
CHECK_NAME = "ServiceNow change approved"
CONNECTION = "ServiceNow approval bridge"
# The headers Microsoft's docs list for a callback check, plus the run's id, which the bridge reads the run by.
HEADERS = {"Content-Type": "application/json", "PlanUrl": "$(system.CollectionUri)", "ProjectId": "$(system.TeamProjectId)",
           "HubName": "$(system.HostType)", "PlanId": "$(system.PlanId)", "JobId": "$(system.JobId)",
           "TimelineId": "$(system.TimelineId)", "TaskInstanceId": "$(system.TaskInstanceId)",
           "AuthToken": "$(system.AccessToken)", "BuildId": "$(build.BuildId)"}
BUILD_NAMESPACE = "33344d9c-fc72-4d6f-aba5-fa317101a7e9"
# View builds, queue builds, stop builds, view the pipeline: queue it, read and cancel its runs.
RELEASE_BOT_RIGHTS = 1 | 128 | 512 | 1024
PROJECT_NAMESPACE = "52d39943-cb85-4d7f-8fa8-c6baac873819"
# View project-level information (GENERIC_READ): a member of a new project group doesn't have it, and Azure
# DevOps answers 404 for a project its caller can't see. Readers and Contributors have it, with much more.
VIEW_PROJECT = 1
RELEASE_BOT = "release-demo Release Bot"
GROUP = "Release bots"


class DevOps:
    """The organization, as you: az account get-access-token for Azure DevOps."""

    def __init__(self, organization):
        self.organization = organization
        result = subprocess.run(["az", "account", "get-access-token", "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
                                 "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True)
        if result.returncode != 0:
            fail("No Azure DevOps token from az: az login, with your es.nl account.")
        self.token = result.stdout.strip()

    def call(self, method, url, body=None, missing_ok=False):
        if not url.startswith("https://"):
            url = f"https://dev.azure.com/{self.organization}{url}"
        request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            fail(f"Azure DevOps {method} {url.split('?')[0]}: {error.code} {error.read().decode(errors='replace')[:300]}")

    def project(self):
        project = self.call("GET", f"/_apis/projects/{PROJECT}?{API}", missing_ok=True)
        if not project:
            fail(f"No project {PROJECT} in {self.organization}: make it first, private.")
        return project


def ensure_pool(devops, project):
    pools = devops.call("GET", f"/_apis/distributedtask/pools?poolName={POOL}&{API}")["value"]
    pool = pools[0] if pools else devops.call("POST", f"/_apis/distributedtask/pools?{API}",
                                              {"name": POOL, "autoProvision": False, "autoUpdate": False})
    if pool.get("autoUpdate"):
        devops.call("PATCH", f"/_apis/distributedtask/pools/{pool['id']}?{API}", {"autoUpdate": False})
    queues = devops.call("GET", f"/{PROJECT}/_apis/distributedtask/queues?queueName={POOL}&{API}")["value"]
    queue = queues[0] if queues else devops.call("POST", f"/{PROJECT}/_apis/distributedtask/queues?{API}",
                                                 {"name": POOL, "pool": {"id": pool["id"]}})
    print(f"pool {POOL}: self-hosted, automatic agent updates off, in the project")
    return pool, queue


def ensure_environment(devops, name, description):
    found = devops.call("GET", f"/{PROJECT}/_apis/pipelines/environments?name={urllib.parse.quote(name)}&{API}")["value"]
    environment = found[0] if found else devops.call("POST", f"/{PROJECT}/_apis/pipelines/environments?{API}",
                                                     {"name": name, "description": description})
    print(f"environment {name}: there")
    return environment


def ensure_pipeline(devops, name, path, repository):
    for pipeline in devops.call("GET", f"/{PROJECT}/_apis/pipelines?{API}")["value"]:
        if pipeline["name"] == name:
            print(f"pipeline {name}: there (id {pipeline['id']})")
            return pipeline
    connections = devops.call("GET", f"/{PROJECT}/_apis/serviceendpoint/endpoints?type=github&{API}")["value"]
    if not connections:
        fail("No GitHub service connection in the project yet: Pipelines, New pipeline, GitHub, install the Azure "
             "Pipelines GitHub App on the repository only, and stop at the review step. Then run this again.")
    pipeline = devops.call("POST", f"/{PROJECT}/_apis/pipelines?{API}", {
        "name": name, "folder": "\\", "configuration": {"type": "yaml", "path": path, "repository": {
            "fullName": repository, "type": "gitHub", "connection": {"id": connections[0]["id"]}}}})
    print(f"pipeline {name}: created from {path} (id {pipeline['id']})")
    return pipeline


def ensure_connection(devops, project, bridge_url):
    found = devops.call("GET", f"/{PROJECT}/_apis/serviceendpoint/endpoints?endpointNames={urllib.parse.quote(CONNECTION)}&{API}")["value"]
    if found:
        return found[0]
    connection = devops.call("POST", f"/_apis/serviceendpoint/endpoints?{API}", {
        "name": CONNECTION, "type": "generic", "url": bridge_url, "isShared": False,
        "description": "The release-demo approval bridge: production's check asks it whether ServiceNow approved.",
        "authorization": {"scheme": "UsernamePassword", "parameters": {"username": "", "password": ""}},
        "serviceEndpointProjectReferences": [{"projectReference": {"id": project["id"], "name": PROJECT},
                                              "name": CONNECTION}]})
    print(f"service connection {CONNECTION}: created")
    return connection


def checks(devops, environment):
    reply = devops.call("GET", f"/{PROJECT}/_apis/pipelines/checks/configurations?resourceType=environment"
                               f"&resourceId={environment['id']}&$expand=settings&{PREVIEW}")
    return reply.get("value", [])


def check_body(environment, connection):
    return {"type": TASK_CHECK, "timeout": 60,
            "settings": {"definitionRef": INVOKE_REST_API, "displayName": CHECK_NAME, "retryInterval": 0,
                         "inputs": {"connectedServiceNameSelector": "connectedServiceName",
                                    "connectedServiceName": connection["id"], "method": "POST",
                                    "headers": json.dumps(HEADERS, indent=1), "body": "",
                                    "urlSuffix": "azure-devops/check", "waitForCompletion": "true"}},
            "resource": {"type": "environment", "id": str(environment["id"]), "name": environment["name"]}}


def ensure_check(devops, environment, connection):
    if any(c.get("settings", {}).get("displayName") == CHECK_NAME for c in checks(devops, environment)):
        print(f"{environment['name']}: the check '{CHECK_NAME}' is there")
        return
    devops.call("POST", f"/{PROJECT}/_apis/pipelines/checks/configurations?{PREVIEW}", check_body(environment, connection))
    print(f"{environment['name']}: the check '{CHECK_NAME}', Invoke REST API, callback, 60 minutes")


def only_pipeline(devops, kind, resource_id, pipeline):
    devops.call("PATCH", f"/{PROJECT}/_apis/pipelines/pipelinepermissions/{kind}/{resource_id}?{PREVIEW}",
                {"allPipelines": {"authorized": False}, "pipelines": [{"id": pipeline["id"], "authorized": True}]})


def release_bot_object_id():
    result = subprocess.run(["az", "ad", "sp", "list", "--display-name", RELEASE_BOT, "--query", "[0].id", "-o", "tsv"],
                            capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        fail(f"No service principal {RELEASE_BOT} in your tenant: apply infra/demos/22-azure-devops-approval first.")
    return result.stdout.strip()


def project_token(project_id):
    """The Project security namespace's token for one project."""
    return f"$PROJECT:vstfs:///Classification/TeamProject/{project_id}"


def graph_urls(devops):
    return f"https://vsaex.dev.azure.com/{devops.organization}", f"https://vssps.dev.azure.com/{devops.organization}"


def release_bot_in_organization(devops):
    """Release Bot's graph entry: the one already in the organization, or added now with Basic access. Adding
    it again isn't asked of Azure DevOps, so --apply can run twice."""
    vsaex, vssps = graph_urls(devops)
    object_id = release_bot_object_id()
    known = devops.call("GET", f"{vssps}/_apis/graph/serviceprincipals?{PREVIEW}")["value"]
    bot = next((s for s in known if s.get("originId") == object_id), None)
    if bot:
        print(f"{RELEASE_BOT}: in the organization")
        return bot
    reply = devops.call("POST", f"{vsaex}/_apis/serviceprincipalentitlements?{PREVIEW}", {
        "accessLevel": {"accountLicenseType": "express"},
        "servicePrincipal": {"origin": "aad", "originId": object_id, "subjectKind": "servicePrincipal"}})
    bot = (reply.get("operationResult") or {}).get("result", reply).get("servicePrincipal", {})
    if not bot.get("descriptor"):
        fail(f"Azure DevOps didn't add {RELEASE_BOT}: {json.dumps(reply)[:300]}")
    print(f"{RELEASE_BOT}: in the organization, Basic access")
    return bot


def release_bots_group(devops, project, create=False):
    """The project group Release bots and the identity descriptor its permissions name, or (None, None)."""
    _, vssps = graph_urls(devops)
    scope = devops.call("GET", f"{vssps}/_apis/graph/descriptors/{project['id']}?{PREVIEW}")["value"]
    groups = devops.call("GET", f"{vssps}/_apis/graph/groups?scopeDescriptor={scope}&{PREVIEW}")["value"]
    group = next((g for g in groups if g["displayName"] == GROUP), None)
    if group is None and create:
        group = devops.call("POST", f"{vssps}/_apis/graph/groups?scopeDescriptor={scope}&{PREVIEW}", {
            "displayName": GROUP,
            "description": "Release Bot: sees the project, queues the release pipeline, reads and cancels its runs."})
    if group is None:
        return None, None
    identity = devops.call("GET", f"{vssps}/_apis/identities?subjectDescriptors={group['descriptor']}&{API}")["value"][0]
    return group, identity["descriptor"]


def release_bot_grants(project, pipeline_id):
    """Release bots' rights, as (security namespace, token, bits): see the project, and queue, read and
    cancel the release pipeline's runs."""
    return [(PROJECT_NAMESPACE, project_token(project["id"]), VIEW_PROJECT),
            (BUILD_NAMESPACE, f"{project['id']}/{pipeline_id}", RELEASE_BOT_RIGHTS)]


def allowed(devops, namespace, token, descriptor):
    """The bits allowed to one identity on one token, as set there."""
    reply = devops.call("GET", f"/_apis/accesscontrollists/{namespace}?token={urllib.parse.quote(token, safe='')}"
                               f"&descriptors={urllib.parse.quote(descriptor, safe='')}&{API}")
    bits = 0
    for acl in reply.get("value", []):
        for ace in (acl.get("acesDictionary") or {}).values():
            bits |= ace.get("allow", 0)
    return bits


def ensure_release_bot(devops, project, pipeline):
    _, vssps = graph_urls(devops)
    bot = release_bot_in_organization(devops)
    group, descriptor = release_bots_group(devops, project, create=True)
    devops.call("PUT", f"{vssps}/_apis/graph/memberships/{bot['descriptor']}/{group['descriptor']}?{PREVIEW}")
    for namespace, token, bits in release_bot_grants(project, pipeline["id"]):
        devops.call("POST", f"/_apis/accesscontrolentries/{namespace}?{API}", {
            "token": token, "merge": True,
            "accessControlEntries": [{"descriptor": descriptor, "allow": bits, "deny": 0}]})
    print(f"{RELEASE_BOT}: in {GROUP}, which may see the project, queue {pipeline['name']}, read and cancel its runs")


def release_bot_problems(devops, project, pipeline_id):
    """--check's lines for Release bots: the group, Release Bot in it, and both rights."""
    group, descriptor = release_bots_group(devops, project)
    if group is None:
        print(f"{GROUP}: missing")
        return [f"no project group {GROUP}: run --apply"]
    _, vssps = graph_urls(devops)
    members = devops.call("GET", f"{vssps}/_apis/graph/memberships/{group['descriptor']}?direction=down&{PREVIEW}")["value"]
    bots = [s["descriptor"] for s in devops.call("GET", f"{vssps}/_apis/graph/serviceprincipals?{PREVIEW}")["value"]
            if s.get("displayName") == RELEASE_BOT]
    member = any(m.get("memberDescriptor") in bots for m in members)
    (project_ns, project_tok, view), (build_ns, build_tok, rights) = release_bot_grants(project, pipeline_id)
    sees = (allowed(devops, project_ns, project_tok, descriptor) & view) == view
    runs = (allowed(devops, build_ns, build_tok, descriptor) & rights) == rights
    print(f"{GROUP}: Release Bot {'in it' if member else 'not in it'}, "
          f"{'sees' if sees else 'does not see'} the project, "
          f"{'may' if runs else 'may not'} queue, read and cancel the release pipeline's runs")
    problems = [] if member else [f"{RELEASE_BOT} isn't in {GROUP}: run --apply"]
    problems += [] if sees else [f"{GROUP} can't see the project, so Azure DevOps answers Release Bot 404: run --apply"]
    problems += [] if runs else [f"{GROUP} may not queue, read and cancel the release pipeline: run --apply"]
    return problems


def apply(devops, bridge_test):
    ssm = ssm_client()
    put_parameter(ssm, "azure-devops/organization", devops.organization, secure=False)
    project = devops.project()
    put_parameter(ssm, "azure-devops/project-id", project["id"], secure=False)
    repository = get_parameter(ssm, "github/repository")
    bridge_url = ssm.get_parameter(Name=SSM_PREFIX + "bridge/url")["Parameter"]["Value"]
    if bridge_url == "unset":
        fail("The approval bridge isn't applied yet: no bridge/url in SSM. Apply infra/ first.")
    _, queue = ensure_pool(devops, project)
    staging = ensure_environment(devops, "staging", "orders-service in staging. No checks.")
    production = ensure_environment(devops, "production", "orders-service in production. No approvers: one check "
                                                          "asks ServiceNow, through the approval bridge.")
    connection = ensure_connection(devops, project, bridge_url)
    ensure_check(devops, production, connection)
    pipeline = ensure_pipeline(devops, "release", "/azure-pipelines.yml", repository)
    put_parameter(ssm, "azure-devops/pipeline-id", str(pipeline["id"]), secure=False)
    for kind, resource in (("environment", staging["id"]), ("environment", production["id"]), ("queue", queue["id"])):
        only_pipeline(devops, kind, resource, pipeline)
    print("staging, production and the pool: the release pipeline only")
    ensure_release_bot(devops, project, pipeline)
    if bridge_test:
        test = ensure_environment(devops, "bridge-test", "Day one only: production's twin, for the bridge's first check.")
        ensure_check(devops, test, connection)
        throwaway = ensure_pipeline(devops, "bridge-test", "/scripts/demos/azure_devops/bridge-test.yml", repository)
        for kind, resource in (("environment", test["id"]), ("queue", queue["id"])):
            devops.call("PATCH", f"/{PROJECT}/_apis/pipelines/pipelinepermissions/{kind}/{resource}?{PREVIEW}",
                        {"pipelines": [{"id": throwaway["id"], "authorized": True}]})
        print("bridge-test: environment, check and throwaway pipeline")


def check(devops):
    project, problems = devops.project(), []
    pipeline_id = get_parameter(ssm_client(), "azure-devops/pipeline-id")
    for name in ("staging", "production"):
        found = devops.call("GET", f"/{PROJECT}/_apis/pipelines/environments?name={name}&{API}")["value"]
        if not found:
            problems.append(f"no environment {name}")
            continue
        environment = found[0]
        configured = checks(devops, environment)
        approvals = [c for c in configured if c["type"]["id"] == APPROVAL_CHECK]
        tasks = [c for c in configured if c["type"]["id"] == TASK_CHECK["id"]]
        print(f"{name}: approvals {len(approvals) or 'none'}, checks {len(configured)}")
        for c in tasks:
            inputs = c["settings"].get("inputs", {})
            print(f"  check '{c['settings'].get('displayName')}': Invoke REST API, completion event "
                  f"{'Callback' if inputs.get('waitForCompletion') == 'true' else 'ApiResponse'}, time between "
                  f"evaluations {c['settings'].get('retryInterval', 0)}, timeout {c.get('timeout')} minutes")
        if approvals:
            problems.append(f"{name} has approvals")
        if name == "staging" and configured:
            problems.append("staging has checks")
        if name == "production":
            wanted = [c for c in tasks if c["settings"].get("displayName") == CHECK_NAME
                      and c["settings"].get("inputs", {}).get("waitForCompletion") == "true"
                      and not c["settings"].get("retryInterval") and c.get("timeout") == 60]
            if len(configured) != 1 or not wanted:
                problems.append(f"production doesn't have '{CHECK_NAME}' alone, callback, 60 minutes")
            allowed = devops.call("GET", f"/{PROJECT}/_apis/pipelines/pipelinepermissions/environment/{environment['id']}?{PREVIEW}")
            pipelines = [str(p["id"]) for p in allowed.get("pipelines", []) if p.get("authorized")]
            everyone = (allowed.get("allPipelines") or {}).get("authorized")
            print(f"  may use it: {'every pipeline' if everyone else 'pipelines ' + ', '.join(pipelines)}")
            if everyone or pipelines != [pipeline_id]:
                problems.append("production is open to more than the release pipeline")
            roles = devops.call("GET", f"/_apis/securityroles/scopes/distributedtask.environmentreferencerole/"
                                       f"roleassignments/resources/{project['id']}_{environment['id']}?{PREVIEW}")
            admins = [r["identity"]["displayName"] for r in roles.get("value", []) if r["role"]["name"] == "Administrator"]
            print(f"  Administrator role (may bypass a check): {', '.join(admins) or 'none'}")
    groups = devops.call("GET", f"/{PROJECT}/_apis/distributedtask/variablegroups?{API}").get("value", [])
    files = devops.call("GET", f"/{PROJECT}/_apis/distributedtask/securefiles?{PREVIEW}").get("value", [])
    definition = devops.call("GET", f"/{PROJECT}/_apis/build/definitions/{pipeline_id}?{API}")
    secret = [n for n, v in (definition.get("variables") or {}).items() if v.get("isSecret")]
    print(f"variable groups {len(groups)}, secure files {len(files)}, secret variables {len(secret)}")
    if groups or files or secret:
        problems.append("the project holds variable groups, secure files or secret variables")
    problems += release_bot_problems(devops, project, pipeline_id)
    pools = devops.call("GET", f"/_apis/distributedtask/pools?poolName={POOL}&{API}")["value"]
    agents = devops.call("GET", f"/_apis/distributedtask/pools/{pools[0]['id']}/agents?{API}")["value"] if pools else []
    print(f"pool {POOL}: agents {', '.join(a['name'] + ' ' + a['status'] for a in agents) or 'none'}")
    if [a["name"] for a in agents] != [AGENT]:
        problems.append(f"the pool's agents aren't {AGENT} alone")
    usage = devops.call("GET", f"/_apis/distributedtask/resourceusage?parallelismTag=Private&poolIsHosted=false&{PREVIEW}",
                        missing_ok=True) or {}
    free = (usage.get("resourceLimit") or {}).get("totalCount")
    print(f"self-hosted parallel jobs: {free if free is not None else 'unknown: see Organization settings, Parallel jobs'}")
    if free == 0:
        problems.append("no self-hosted parallel job: linking billing is Mateusz's decision")
    for problem in problems:
        print(f"  FAIL {problem}")
    print("As the demo needs it." if not problems else f"{len(problems)} thing(s) to fix.")
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument("--organization", help="the organization's name, for the first --apply")
    parser.add_argument("--bridge-test", action="store_true", help="also the day-one environment and pipeline")
    args = parser.parse_args()
    organization = args.organization or get_parameter(ssm_client(), "azure-devops/organization")
    devops = DevOps(organization)
    if args.apply:
        apply(devops, args.bridge_test)
    return check(devops)


if __name__ == "__main__":
    sys.exit(main())
