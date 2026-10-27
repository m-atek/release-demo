"""quote-service's six targets: staging and production in AWS, Azure and Google Cloud.

Each target answers the same questions the deploy and check scripts ask, in
its own cloud's terms:

- AWS Lambda: published versions, each described by its app version, and the
  alias live with a function URL;
- Azure Container Apps: revisions in multiple-revision mode, all traffic on
  one, the version in the revision suffix (v1-2-0-b17);
- Google Cloud Run: revisions, all traffic on one, the version in the
  revision name (quote-service-staging-v1-2-0-b17).

Moving back is moving traffic to an earlier revision or version, which takes
seconds and needs no new image.

Each cloud's names and ids come from its Terraform root through SSM
(clouds/aws, clouds/azure, clouds/gcp), never from this public repository.
"""

import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers

from common import fail, ssm_client  # noqa: E402
from identity import acr_password  # noqa: E402
from registry import Registry  # noqa: E402

Revision = namedtuple("Revision", "id version")
SUFFIX = re.compile(r"v(\d+)-(\d+)-(\d+)-[a-z0-9-]+$")


def suffix(version, label):
    """v1-2-0-b17: the version where a revision name can hold it."""
    return f"v{version.replace('.', '-')}-{label}"


def version_in(name):
    match = SUFFIX.search(name or "")
    return ".".join(match.groups()) if match else "unknown"


def cloud_settings(cloud, config, ssm=None):
    """A cloud's ids and names: Terraform's SSM parameter, plus services.yaml."""
    ssm = ssm or ssm_client()
    try:
        raw = ssm.get_parameter(Name=f"/release-demo/clouds/{cloud}")["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        fail(f"{cloud} isn't set up yet: no /release-demo/clouds/{cloud}. See the README, Demo 5.")
    region = next(c["region"] for c in config["clouds"] if c["key"] == cloud)
    return {"region": region, **json.loads(raw)}


class Target:
    key = name = None

    def __init__(self, config, environment, settings, login):
        self.config, self.environment, self.settings, self.login = config, environment, settings, login
        self.target = config["environments"][environment]["targets"][self.key]
        self.resource = self.target["name"]

    def __str__(self):
        return f"{self.name} {self.environment}"

    def image(self, digest):
        return f"{self.settings['registry']}/{self.settings['repository']}@{digest}"

    def previous_release(self, live):
        """The newest revision before the live one with a different version."""
        revisions = self.revisions()
        ids = [r.id for r in revisions]
        older = revisions[ids.index(live.id) + 1:] if live.id in ids else revisions
        return next((r for r in older if r.version not in (live.version, "unknown")), None)

    def find(self, version):
        """The newest revision running a version, for reset.py."""
        return next((r for r in self.revisions() if r.version == version), None)


class AwsLambda(Target):
    key, name = "aws", "AWS"

    def __init__(self, *args):
        super().__init__(*args)
        self.client = self.login.access["session"].client("lambda")

    def registry(self):
        reply = self.login.access["session"].client("ecr").get_authorization_token()["authorizationData"][0]
        user, password = base64.b64decode(reply["authorizationToken"]).decode().split(":", 1)
        return Registry(self.settings["registry"], user, password, name="Amazon ECR")

    def _version(self, number):
        description = self.client.get_function_configuration(FunctionName=self.resource, Qualifier=number)["Description"]
        return description if re.fullmatch(r"\d+\.\d+\.\d+", description) else "unknown"

    def live(self):
        number = self.client.get_alias(FunctionName=self.resource, Name="live")["FunctionVersion"]
        return Revision(number, self._version(number))

    def deploy(self, digest, version, label):
        self.client.update_function_code(FunctionName=self.resource, ImageUri=self.image(digest))
        self.client.get_waiter("function_updated_v2").wait(FunctionName=self.resource)
        published = self.client.publish_version(FunctionName=self.resource, Description=version)
        self.move(published["Version"])
        return Revision(published["Version"], version)

    def move(self, revision_id):
        self.client.update_alias(FunctionName=self.resource, Name="live", FunctionVersion=revision_id)

    def revisions(self):
        versions = []
        for page in self.client.get_paginator("list_versions_by_function").paginate(FunctionName=self.resource):
            versions += [v for v in page["Versions"] if v["Version"] != "$LATEST"]
        versions.sort(key=lambda v: int(v["Version"]), reverse=True)
        return [Revision(v["Version"], v["Description"] if re.fullmatch(r"\d+\.\d+\.\d+", v["Description"]) else "unknown")
                for v in versions]

    def url(self):
        return self.client.get_function_url_config(FunctionName=self.resource, Qualifier="live")["FunctionUrl"].rstrip("/")


class CloudError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def call(method, url, token, body=None, timeout=60):
    """(status, headers, parsed JSON) of one cloud API call."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, headers, raw = error.code, error.headers, error.read()
    reply = json.loads(raw) if raw.strip() else {}
    if status >= 400:
        message = reply.get("error", {}).get("message") if isinstance(reply.get("error"), dict) else reply
        raise CloudError(f"{method} {url.split('?')[0].split('/')[-1]} answered {status}: {str(message)[:400]}", status)
    return status, headers, reply


class AzureContainerApp(Target):
    key, name = "azure", "Azure"
    API = "api-version=2025-07-01"

    def __init__(self, *args):
        super().__init__(*args)
        if not self.settings.get("registry"):
            fail("Azure's registry and apps are down. Start them: scripts/demo.sh up clouds")
        self.base = (f"https://management.azure.com/subscriptions/{self.settings['subscription_id']}/resourceGroups/"
                     f"{self.settings['resource_group']}/providers/Microsoft.App/containerApps/{self.resource}")

    def _call(self, method, path="", body=None):
        separator = "&" if "?" in path else "?"
        return call(method, f"{self.base}{path}{separator}{self.API}", self.login.access["arm"], body)

    def _wait(self, headers, what, timeout=300):
        """Follow an Azure long-running operation to its end."""
        poll = headers.get("Azure-AsyncOperation") or headers.get("Location")
        deadline = time.monotonic() + timeout
        while poll and time.monotonic() < deadline:
            status, _, reply = call("GET", poll, self.login.access["arm"])
            state = reply.get("status") if isinstance(reply, dict) else None
            if state in ("Succeeded", None) and status == 200:
                return
            if state in ("Failed", "Canceled"):
                raise CloudError(f"Azure couldn't {what}: {reply.get('error', {}).get('message', state)}")
            time.sleep(3)
        if poll:
            raise CloudError(f"Azure didn't {what} within {timeout} seconds")

    def app(self):
        return self._call("GET")[2]["properties"]

    def registry(self):
        server = self.settings["registry"]
        return Registry(server, "00000000-0000-0000-0000-000000000000", acr_password(self.login, server),
                        name="Azure Container Registry")

    def live(self):
        app = self.app()
        weights = [t for t in app["configuration"]["ingress"].get("traffic", []) if t.get("weight", 0) > 0]
        top = max(weights, key=lambda t: t["weight"])
        name = app["latestReadyRevisionName"] if top.get("latestRevision") else top["revisionName"]
        return Revision(name, version_in(name))

    def _update(self, template, traffic, what):
        app = self.app()
        configuration = {k: v for k, v in app["configuration"].items() if k != "secrets"}
        configuration["ingress"] = {k: v for k, v in configuration["ingress"].items() if k != "fqdn"}
        configuration["ingress"]["traffic"] = traffic
        status, headers, _ = self._call("PATCH", "", {"properties": {"template": template or app["template"],
                                                                     "configuration": configuration}})
        if status == 202:
            self._wait(headers, what)

    def deploy(self, digest, version, label):
        template = self.app()["template"]
        template["containers"][0]["image"] = self.image(digest)
        template["revisionSuffix"] = suffix(version, label)
        self._update(template, [{"latestRevision": True, "weight": 100}], f"deploy {version}")
        name = f"{self.resource}--{template['revisionSuffix']}"
        self._ready(name)
        return Revision(name, version)

    def _ready(self, name, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                revision = self._call("GET", f"/revisions/{name}")[2]["properties"]
            except CloudError as error:
                if error.status != 404:
                    raise
                time.sleep(3)
                continue
            if revision["provisioningState"] == "Provisioned":
                return
            if revision["provisioningState"] == "Failed":
                raise CloudError(f"Azure couldn't start {name}: {revision.get('provisioningError', 'no reason given')}")
            time.sleep(3)
        raise CloudError(f"Azure didn't start {name} within {timeout} seconds")

    def move(self, revision_id):
        revision = self._call("GET", f"/revisions/{revision_id}")[2]["properties"]
        if not revision.get("active"):
            self._call("POST", f"/revisions/{revision_id}/activate")
            self._ready(revision_id)
        self._update(None, [{"revisionName": revision_id, "weight": 100}], f"move traffic to {revision_id}")

    def revisions(self):
        found = self._call("GET", "/revisions")[2].get("value", [])
        found.sort(key=lambda r: r["properties"]["createdTime"], reverse=True)
        return [Revision(r["name"], version_in(r["name"])) for r in found]

    def url(self):
        return "https://" + self.app()["configuration"]["ingress"]["fqdn"]


class GoogleCloudRun(Target):
    key, name = "gcp", "Google Cloud"
    OUTPUT_ONLY = {"uid", "reconciling", "creator", "generation", "terminalCondition", "latestReadyRevision",
                   "createTime", "uri", "threatDetectionEnabled", "trafficStatuses", "conditions", "deleteTime",
                   "expireTime", "latestCreatedRevision", "observedGeneration", "lastModifier", "satisfiesPzs",
                   "updateTime", "urls"}

    def __init__(self, *args):
        super().__init__(*args)
        if not self.settings.get("registry"):
            fail("Google Cloud's registry and services are down. Start them: scripts/demo.sh up clouds")
        self.base = (f"https://run.googleapis.com/v2/projects/{self.settings['project_id']}/locations/"
                     f"{self.settings['region']}/services/{self.resource}")

    def _call(self, method, url, body=None):
        return call(method, url, self.login.access["token"], body)

    def service(self):
        return self._call("GET", self.base)[2]

    def registry(self):
        return Registry(self.settings["registry"], "oauth2accesstoken", self.login.access["token"],
                        name="Artifact Registry")

    def live(self):
        service = self.service()
        top = max(service.get("trafficStatuses", []), key=lambda t: t.get("percent", 0))
        name = top.get("revision") or service["latestReadyRevision"].split("/")[-1]
        return Revision(name, version_in(name))

    def _update(self, service, what, timeout=300):
        body = {k: v for k, v in service.items() if k not in self.OUTPUT_ONLY}
        operation = self._call("PATCH", self.base, body)[2]
        deadline = time.monotonic() + timeout
        while not operation.get("done"):
            if time.monotonic() > deadline:
                raise CloudError(f"Google Cloud didn't {what} within {timeout} seconds")
            time.sleep(3)
            operation = self._call("GET", f"https://run.googleapis.com/v2/{operation['name']}")[2]
        if operation.get("error"):
            raise CloudError(f"Google Cloud couldn't {what}: {operation['error'].get('message')}")

    def deploy(self, digest, version, label):
        service = self.service()
        service["template"]["containers"][0]["image"] = self.image(digest)
        service["template"]["revision"] = f"{self.resource}-{suffix(version, label)}"
        service["traffic"] = [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}]
        self._update(service, f"deploy {version}")
        return Revision(service["template"]["revision"], version)

    def move(self, revision_id):
        service = self.service()
        service["traffic"] = [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": revision_id, "percent": 100}]
        self._update(service, f"move traffic to {revision_id}")

    def revisions(self):
        found = self._call("GET", f"{self.base}/revisions")[2].get("revisions", [])
        found.sort(key=lambda r: r["createTime"], reverse=True)
        return [Revision(r["name"].split("/")[-1], version_in(r["name"])) for r in found]

    def url(self):
        return self.service()["uri"].rstrip("/")


KINDS = {"aws": AwsLambda, "azure": AzureContainerApp, "gcp": GoogleCloudRun}


def target(cloud, config, environment, settings, login):
    return KINDS[cloud](config, environment, settings, login)
