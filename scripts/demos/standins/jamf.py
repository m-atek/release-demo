"""Jamf Pro, real or stand-in, behind one small interface. Used by demo 6
(Store Assistant, a Mac app through a policy) and demo 7 (Store Floor, an
in-house iOS app).

The client, for pipelines:

    jamf = jamf.connect()                      # STANDIN from the environment, else the stand-in
    jamf.upload_package(path)                  # Jamf Pro API: create the package record, upload the file
    policy = jamf.policy("Store Assistant")    # Classic API: package, scope, trigger, frequency
    jamf.set_policy_package(policy["id"], package["id"])
    jamf.set_policy_scope(policy["id"], ["Pilot"])
    jamf.flush_policy_logs(policy["id"])       # DELETE /JSSResource/logflush/policy/id/<id>/interval/Zero+Days
    jamf.app_versions("Pilot", "nl.es.StoreAssistant")    # inventory: who reports which version

    app = jamf.mobile_app("Store Floor")       # Classic API: in-house app, version, scope
    jamf.upload_ipa(app["id"], path)           # POST /JSSResource/fileuploads/mobiledeviceapplicationsipa/id/<id>
    jamf.set_mobile_app_scope(app["id"], ["Pilot"])
    jamf.mobile_app_versions("Pilot", "nl.es.StoreFloor")

connect() reads the URL and an API client's id and secret from SSM: jamf/url,
jamf/client-id and jamf/client-secret for the real Jamf Pro, or the
jamf/standin-* values Terraform writes for the stand-in. Everything else is
the same requests against either.

The stand-in, for when there is no Jamf Pro (its trial lasts 14 days): an
AWS Lambda function behind a function URL (infra/demos/6-desktop), keeping its
state and files in S3. It answers the requests above the way Jamf Pro does,
plus a few of its own under /standin/:

- the pilot Mac's agent (jamf-agent.sh, installed as /usr/local/bin/jamf):
  enroll, check in, report policy logs and inventory. sudo jamf policy runs
  the policies in scope, as the real binary does;
- simulated devices: computers and mobile devices with no agent, which
  install what is scoped to them as soon as it is, so a group has members
  beyond the one real pilot Mac;
- a page with each policy and app, its scope and the inventory of the groups
  it is scoped to, refreshed every 2 seconds (python jamf.py ui-link gives a
  signed link to it);
- uploads over 5 MB go to S3 through a signed URL first: a Lambda function
  URL takes requests up to 6 MB.

The stand-in says what it is in its startup log; nothing else marks it.

    python jamf.py serve --dir .local/jamf --port 8090     the stand-in on this Mac, for development and tests
    python jamf.py ui-link                                  a signed link to the stand-in's page, valid 12 hours
    python jamf.py status                                   policies, apps, scope and inventory, in the terminal

Standard library only (boto3 on Lambda and for SSM).
"""

import base64
import datetime
import email
import hashlib
import hmac
import io
import json
import os
import plistlib
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

UPLOAD_LIMIT = 5_500_000  # bytes a request body may carry to the stand-in's Lambda function URL
TOKEN_SECONDS = 1200
UI_SECONDS = 12 * 3600


# The client

class JamfError(Exception):
    pass


class Jamf:
    """The Jamf Pro API and Classic API calls the demos make, with an API client's token."""

    def __init__(self, url, client_id, client_secret, standin=False):
        self.url = url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.standin = standin
        self._token = None
        self._expires = 0

    # HTTP

    def token(self):
        if self._token is None or time.time() > self._expires - 60:
            body = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                                           "grant_type": "client_credentials"}).encode()
            reply = self._send("POST", "/api/oauth/token", body, {"Content-Type": "application/x-www-form-urlencoded"},
                               auth=False)
            data = json.loads(reply)
            self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 300))
        return self._token

    def _send(self, method, path, body=None, headers=None, auth=True, timeout=120):
        headers = dict(headers or {})
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise JamfError(f"Jamf {method} {path.split('?')[0]}: {error.code} {error.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as error:
            raise JamfError(f"Jamf at {self.url} doesn't answer: {error.reason}")

    def api(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        raw = self._send(method, path, body, {"Accept": "application/json", "Content-Type": "application/json"})
        return json.loads(raw) if raw else None

    def classic(self, method, path, element=None):
        body = ET.tostring(element, encoding="utf-8") if element is not None else None
        try:
            raw = self._send(method, "/JSSResource" + path, body, {"Accept": "application/xml", "Content-Type": "application/xml"})
        except JamfError as error:
            if method == "GET" and ": 404 " in str(error):
                return None
            raise
        return ET.fromstring(raw) if raw and raw.strip() else None

    def _multipart(self, path, field, filename, data):
        if self.standin and len(data) > UPLOAD_LIMIT:
            # Too big for the stand-in's Lambda function URL: the file goes to S3 first.
            ticket = self.api("POST", "/standin/v1/uploads", {"fileName": filename})
            put = urllib.request.Request(ticket["url"], data=data, method="PUT")
            urllib.request.urlopen(put, timeout=300).read()
            return self._send("POST", path, b"", {"X-Standin-Upload": ticket["key"]})
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self._send("POST", path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                               "Accept": "application/json"}, timeout=600)

    # Packages

    def package(self, name):
        query = urllib.parse.urlencode({"page": 0, "page-size": 100, "filter": f'packageName=="{name}"'})
        results = self.api("GET", f"/api/v1/packages?{query}")["results"]
        return results[0] if results else None

    def package_by_id(self, package_id):
        return self.api("GET", f"/api/v1/packages/{package_id}")

    def upload_package(self, path, name=None):
        """Create the package record if needed and upload the file to it. Returns the record."""
        path = Path(path)
        name = name or path.name
        record = self.package(name)
        if record is None:
            created = self.api("POST", "/api/v1/packages", {
                "packageName": name, "fileName": path.name, "categoryId": "-1", "priority": 10,
                "fillUserTemplate": False, "uninstall": False, "rebootRequired": False, "osInstall": False,
                "suppressUpdates": False, "suppressFromDock": False, "suppressEula": False, "suppressRegistration": False})
            package_id = created["id"]
        else:
            package_id = record["id"]
        self._multipart(f"/api/v1/packages/{package_id}/upload", "file", path.name, path.read_bytes())
        return self.package_by_id(package_id)

    # Policies

    def policy(self, name):
        element = self.classic("GET", f"/policies/name/{urllib.parse.quote(name)}")
        return parse_policy(element) if element is not None else None

    def create_policy(self, name, groups, recon=True):
        policy = ET.Element("policy")
        general = sub(policy, "general")
        sub(general, "name", name)
        sub(general, "enabled", "true")
        sub(general, "trigger", "EVENT")
        sub(general, "trigger_checkin", "true")
        sub(general, "frequency", "Once per computer")
        policy.append(scope_element("computer", groups))
        sub(sub(policy, "maintenance"), "recon", "true" if recon else "false")
        reply = self.classic("POST", "/policies/id/0", policy)
        return int(reply.findtext("id"))

    def set_policy_package(self, policy_id, package_id):
        policy = ET.Element("policy")
        packages = sub(sub(policy, "package_configuration"), "packages")
        sub(packages, "size", "1")
        package = sub(packages, "package")
        sub(package, "id", str(package_id))
        sub(package, "action", "Install")
        self.classic("PUT", f"/policies/id/{policy_id}", policy)

    def set_policy_scope(self, policy_id, groups):
        policy = ET.Element("policy")
        policy.append(scope_element("computer", groups))
        self.classic("PUT", f"/policies/id/{policy_id}", policy)

    def flush_policy_logs(self, policy_id):
        """Forget where the policy ran, so a once-per-computer policy runs again."""
        self._send("DELETE", f"/JSSResource/logflush/policy/id/{policy_id}/interval/Zero+Days", None,
                   {"Accept": "application/xml"})

    # Computer groups and inventory

    def computer_group(self, name):
        element = self.classic("GET", f"/computergroups/name/{urllib.parse.quote(name)}")
        return parse_group(element, "computer") if element is not None else None

    def create_smart_computer_group(self, name, criteria):
        self.classic("POST", "/computergroups/id/0", group_element("computer", name, criteria))

    def computers(self):
        """Every computer with its applications: id, name, last contact, applications."""
        found, page = [], 0
        while True:
            query = urllib.parse.urlencode([("section", "GENERAL"), ("section", "APPLICATIONS"),
                                            ("page", page), ("page-size", 200)])
            reply = self.api("GET", f"/api/v1/computers-inventory?{query}")
            for item in reply["results"]:
                general = item.get("general") or {}
                found.append({"id": str(item["id"]), "name": general.get("name"),
                              "last_contact": general.get("lastContactTime"),
                              "applications": item.get("applications") or []})
            page += 1
            if page * 200 >= reply["totalCount"]:
                return found

    def app_versions(self, group, bundle_id):
        """The version of an app on each member of a computer group (None where it's missing)."""
        members = {str(m["id"]) for m in self.computer_group(group)["members"]}
        rows = []
        for computer in self.computers():
            if computer["id"] in members:
                app = next((a for a in computer["applications"] if a.get("bundleId") == bundle_id), None)
                rows.append({"id": computer["id"], "name": computer["name"], "last_contact": computer["last_contact"],
                             "version": app.get("version") if app else None})
        return rows

    # Mobile devices, for demo 7

    def mobile_app(self, name):
        element = self.classic("GET", f"/mobiledeviceapplications/name/{urllib.parse.quote(name)}")
        return parse_mobile_app(element) if element is not None else None

    def create_mobile_app(self, name, bundle_id, groups, version="0"):
        app = ET.Element("mobile_device_application")
        general = sub(app, "general")
        for key, value in (("name", name), ("display_name", name), ("bundle_id", bundle_id), ("version", version),
                           ("internal_app", "true"), ("deploy_automatically", "true"),
                           ("deploy_as_managed_app", "true")):
            sub(general, key, value)
        app.append(scope_element("mobile_device", groups))
        reply = self.classic("POST", "/mobiledeviceapplications/id/0", app)
        return int(reply.findtext("id"))

    def upload_ipa(self, app_id, path):
        path = Path(path)
        self._multipart(f"/JSSResource/fileuploads/mobiledeviceapplicationsipa/id/{app_id}", "name", path.name,
                        path.read_bytes())

    def set_mobile_app_scope(self, app_id, groups):
        app = ET.Element("mobile_device_application")
        app.append(scope_element("mobile_device", groups))
        self.classic("PUT", f"/mobiledeviceapplications/id/{app_id}", app)

    def mobile_device_group(self, name):
        element = self.classic("GET", f"/mobiledevicegroups/name/{urllib.parse.quote(name)}")
        return parse_group(element, "mobile_device") if element is not None else None

    def create_smart_mobile_group(self, name, criteria):
        self.classic("POST", "/mobiledevicegroups/id/0", group_element("mobile_device", name, criteria))

    def mobile_app_versions(self, group, bundle_id):
        members = {str(m["id"]) for m in self.mobile_device_group(group)["members"]}
        query = urllib.parse.urlencode([("section", "GENERAL"), ("section", "APPLICATIONS"), ("page-size", 500)])
        rows = []
        for device in self.api("GET", f"/api/v2/mobile-devices/detail?{query}")["results"]:
            if str(device["mobileDeviceId"]) in members:
                app = next((a for a in device.get("applications") or [] if a.get("identifier") == bundle_id), None)
                rows.append({"id": str(device["mobileDeviceId"]), "name": (device.get("general") or {}).get("displayName"),
                             "version": app.get("shortVersion") or app.get("version") if app else None})
        return rows

    # Stand-in only

    def add_simulated_device(self, kind, name, applications=None):
        """A computer or mobile device with no agent: it installs what's scoped to it at once.
        With applications, an existing one is put back on exactly those (for reset scripts)."""
        request = {"kind": kind, "name": name}
        if applications is not None:
            request["applications"] = list(applications)
        return self.api("POST", "/standin/v1/simulated-devices", request)


def sub(parent, tag, text=None):
    element = ET.SubElement(parent, tag)
    if text is not None:
        element.text = str(text)
    return element


def scope_element(kind, groups):
    """<scope> targeting these groups only: all_<kind>s false, no single devices."""
    scope = ET.Element("scope")
    sub(scope, f"all_{kind}s", "false")
    sub(scope, f"{kind}s")
    targets = sub(scope, f"{kind}_groups")
    for name in groups:
        sub(sub(targets, f"{kind}_group"), "name", name)
    return scope


def group_element(kind, name, criteria):
    """A smart group: criteria as (field, search type, value) tuples, joined with and."""
    group = ET.Element(f"{kind}_group")
    sub(group, "name", name)
    sub(group, "is_smart", "true")
    element = sub(group, "criteria")
    sub(element, "size", str(len(criteria)))
    for priority, (field, search_type, value) in enumerate(criteria):
        criterion = sub(element, "criterion")
        sub(criterion, "name", field)
        sub(criterion, "priority", str(priority))
        sub(criterion, "and_or", "and")
        sub(criterion, "search_type", search_type)
        sub(criterion, "value", value)
    return group


def parse_scope(scope, kind):
    if scope is None:
        return {"all": False, "groups": []}
    return {"all": scope.findtext(f"all_{kind}s") == "true",
            "groups": [g.findtext("name") for g in scope.findall(f"{kind}_groups/{kind}_group")]}


def parse_policy(element):
    return {
        "id": int(element.findtext("general/id")),
        "name": element.findtext("general/name"),
        "enabled": element.findtext("general/enabled") == "true",
        "trigger_checkin": element.findtext("general/trigger_checkin") == "true",
        "frequency": element.findtext("general/frequency"),
        "packages": [{"id": int(p.findtext("id")), "name": p.findtext("name"), "action": p.findtext("action")}
                     for p in element.findall("package_configuration/packages/package")],
        "scope": parse_scope(element.find("scope"), "computer"),
        "recon": element.findtext("maintenance/recon") == "true",
    }


def parse_group(element, kind):
    return {"id": int(element.findtext("id")), "name": element.findtext("name"),
            "is_smart": element.findtext("is_smart") == "true",
            "members": [{"id": int(m.findtext("id")), "name": m.findtext("name")}
                        for m in element.findall(f"{kind}s/{kind}")]}


def parse_mobile_app(element):
    return {"id": int(element.findtext("general/id")), "name": element.findtext("general/name"),
            "bundle_id": element.findtext("general/bundle_id"), "version": element.findtext("general/version"),
            "scope": parse_scope(element.find("scope"), "mobile_device")}


def standin_mode():
    return os.environ.get("STANDIN", "1") not in ("0", "false", "no", "")


def connect(standin=None, ssm=None):
    """A client for the real Jamf Pro or the stand-in, with values from SSM."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common import get_parameter, ssm_client

    standin = standin_mode() if standin is None else standin
    ssm = ssm or ssm_client()
    prefix = "jamf/standin-" if standin else "jamf/"
    return Jamf(get_parameter(ssm, prefix + "url"), get_parameter(ssm, prefix + "client-id"),
                get_parameter(ssm, prefix + "client-secret"), standin=standin)


# What packages and .ipa files install

def xar_files(data):
    """The files of a xar archive (a flat .pkg): {path: bytes}, gzip-encoded data inflated."""
    if data[:4] != b"xar!":
        return {}
    header_size, _, toc_length = struct.unpack(">HHQ", data[4:16])
    toc = ET.fromstring(zlib.decompress(data[header_size:header_size + toc_length]))
    heap = header_size + toc_length
    files = {}

    def walk(element, prefix):
        for node in element.findall("file"):
            path = f"{prefix}{node.findtext('name')}"
            content = node.find("data")
            if content is not None:
                offset, length = int(content.findtext("offset")), int(content.findtext("length"))
                raw = data[heap + offset:heap + offset + length]
                encoding = content.find("encoding")
                style = encoding.get("style") if encoding is not None else ""
                if "gzip" in style:
                    raw = zlib.decompress(raw)
                elif "bzip2" in style:
                    import bz2
                    raw = bz2.decompress(raw)
                files[path] = raw
            walk(node, path + "/")

    walk(toc.find("toc"), "")
    return files


def package_contents(data):
    """The apps a flat .pkg installs: [{name, version, bundleId, path}], from its PackageInfo files."""
    apps = []
    for path, content in xar_files(data).items():
        if path.endswith("PackageInfo"):
            info = ET.fromstring(content)
            for bundle in info.iter("bundle"):
                bundle_path = bundle.get("path", "").lstrip(".")
                if bundle_path.endswith(".app"):
                    apps.append({"name": Path(bundle_path).stem, "bundleId": bundle.get("id"),
                                 "version": bundle.get("CFBundleShortVersionString") or info.get("version"),
                                 "path": bundle_path})
    return apps


def ipa_contents(data):
    """The app an .ipa holds: {name, bundleId, version, build}, from Payload/*.app/Info.plist."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name):
                info = plistlib.loads(archive.read(name))
                return {"name": info.get("CFBundleDisplayName") or info.get("CFBundleName"),
                        "bundleId": info.get("CFBundleIdentifier"), "version": info.get("CFBundleShortVersionString"),
                        "build": info.get("CFBundleVersion")}
    raise ValueError("No Payload/<app>.app/Info.plist in the .ipa")


# The stand-in's storage: S3 on Lambda, a folder on this Mac

class LocalStore:
    def __init__(self, directory, base_url=""):
        self.directory = Path(directory)
        (self.directory / "files").mkdir(parents=True, exist_ok=True)
        self.base_url = base_url

    def load(self):
        path = self.directory / "state.json"
        return (json.loads(path.read_text()), None) if path.exists() else (None, None)

    def save(self, state, version):
        temp = self.directory / "state.json.tmp"
        temp.write_text(json.dumps(state, indent=1))
        temp.replace(self.directory / "state.json")

    def put_file(self, key, data):
        path = self.directory / "files" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_file(self, key):
        return (self.directory / "files" / key).read_bytes()

    def download_url(self, key, signer):
        return f"{self.base_url}/standin/files/{urllib.parse.quote(key)}?sig={signer.sign('file.' + key, 900)}"

    def upload_url(self, key, signer):
        return f"{self.base_url}/standin/files/{urllib.parse.quote(key)}?sig={signer.sign('upload.' + key, 900)}"


class S3Store:
    def __init__(self, bucket, client=None):
        import boto3
        self.bucket = bucket
        self.s3 = client or boto3.client("s3")

    def load(self):
        try:
            reply = self.s3.get_object(Bucket=self.bucket, Key="state.json")
        except self.s3.exceptions.NoSuchKey:
            return None, None
        return json.loads(reply["Body"].read()), reply["ETag"]

    def save(self, state, version):
        """Only over the version this request read: two writers can't lose each other's change."""
        from botocore.exceptions import ParamValidationError
        body = json.dumps(state).encode()
        condition = {"IfNoneMatch": "*"} if version is None else {"IfMatch": version}
        try:
            self.s3.put_object(Bucket=self.bucket, Key="state.json", Body=body, **condition)
        except ParamValidationError:  # a boto3 older than S3's conditional writes
            self.s3.put_object(Bucket=self.bucket, Key="state.json", Body=body)

    def put_file(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=f"files/{key}", Body=data)

    def get_file(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=f"files/{key}")["Body"].read()

    def download_url(self, key, signer):
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": f"files/{key}"},
                                              ExpiresIn=900)

    def upload_url(self, key, signer):
        return self.s3.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": f"files/{key}"},
                                              ExpiresIn=900)


class Signer:
    """Stateless tokens: purpose.expiry.signature, HMAC-SHA256 with the stand-in's key."""

    def __init__(self, key):
        self.key = key.encode()

    def sign(self, purpose, seconds):
        expiry = str(int(time.time()) + seconds)
        mac = hmac.new(self.key, f"{purpose}.{expiry}".encode(), hashlib.sha256).hexdigest()[:40]
        return f"{expiry}.{mac}"

    def check(self, purpose, token):
        try:
            expiry, mac = token.split(".", 1)
            wanted = hmac.new(self.key, f"{purpose}.{expiry}".encode(), hashlib.sha256).hexdigest()[:40]
            return hmac.compare_digest(mac, wanted) and int(expiry) > time.time()
        except (ValueError, AttributeError):
            return False


# The stand-in server

class Reply(Exception):
    """Ends a request with this answer."""

    def __init__(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        self.status, self.content_type, self.headers = status, content_type, headers or {}
        self.body = body if isinstance(body, bytes) else str(body).encode()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def empty_state():
    return {"ids": {}, "computers": {}, "mobile_devices": {}, "computer_groups": {}, "mobile_device_groups": {},
            "packages": {}, "policies": {}, "mobile_apps": {}, "activity": []}


class Standin:
    """Jamf Pro's API, as much as the demos use, over a store. One instance per process."""

    def __init__(self, store, client_id, client_secret, signing_key, enrollment_code):
        self.store = store
        self.client_id, self.client_secret = client_id, client_secret
        self.signer = Signer(signing_key)
        self.enrollment_code = enrollment_code
        print("Jamf Pro stand-in: answers the Jamf Pro API and Classic API calls the demos make, "
              "keeps its state in " + type(store).__name__, flush=True)

    # Request handling

    def handle(self, method, path, query, headers, body, base_url):
        """Returns (status, content type, extra headers, body bytes)."""
        headers = {k.lower(): v for k, v in headers.items()}
        self.base_url = base_url
        if hasattr(self.store, "base_url"):
            self.store.base_url = base_url
        try:
            self.route(method, path, query, headers, body)
            raise Reply(404, "Not found")
        except Reply as reply:
            return reply.status, reply.content_type, reply.headers, reply.body
        except ConflictError:
            return 409, "text/plain", {}, b"Changed by another request: retry"

    def route(self, method, path, query, headers, body):
        if path in ("", "/"):
            raise Reply(302, "", headers={"Location": "/standin/ui"})
        if path == "/api/oauth/token" and method == "POST":
            self.oauth(body)
        if path == "/api/v1/jamf-pro-version":
            self.require_api(headers)
            raise json_reply({"version": "11.22.0-standin"})
        if path.startswith("/standin/agent/"):
            self.agent(method, path, headers, body)
        if path.startswith("/standin/files/"):
            self.files(method, path, query, body)
        if path.startswith("/standin/ui"):
            self.ui(path, query, headers)
        self.require_api(headers)
        if path.startswith("/api/"):
            self.pro_api(method, path, query, headers, body)
        if path.startswith("/JSSResource/"):
            self.classic_api(method, path[len("/JSSResource"):], headers, body)
        if path == "/standin/v1/simulated-devices" and method == "POST":
            self.mutate(lambda state: self.add_simulated(state, json.loads(body)), json_result=True)
        if path == "/standin/v1/uploads" and method == "POST":
            key = f"uploads/{uuid.uuid4().hex}/{json.loads(body)['fileName']}"
            raise json_reply({"key": key, "url": self.store.upload_url(key, self.signer)})

    def oauth(self, body):
        form = urllib.parse.parse_qs(body.decode())
        client_id, client_secret = form.get("client_id", [""])[0], form.get("client_secret", [""])[0]
        if not (hmac.compare_digest(client_id, self.client_id) and hmac.compare_digest(client_secret, self.client_secret)):
            raise Reply(401, json.dumps({"error": "invalid_client"}), "application/json")
        raise json_reply({"access_token": "api." + self.signer.sign("api", TOKEN_SECONDS), "scope": "api-role:release-pipeline",
                          "token_type": "Bearer", "expires_in": TOKEN_SECONDS - 1})

    def require_api(self, headers):
        token = headers.get("authorization", "").removeprefix("Bearer ").removeprefix("api.")
        if not self.signer.check("api", token):
            raise Reply(401, json.dumps({"httpStatus": 401, "errors": []}), "application/json")

    # State

    def read(self):
        state, version = self.store.load()
        return state or empty_state(), version

    def mutate(self, change, json_result=False, xml_result=False):
        """Read, change, simulate the agentless devices, write; retried if another request wrote first."""
        for _ in range(5):
            state, version = self.read()
            result = change(state)
            self.simulate(state)
            try:
                self.store.save(state, version)
            except Exception as error:  # S3 says the state changed since this request read it
                if "PreconditionFailed" in str(error) or "ConditionalRequestConflict" in str(error):
                    continue
                raise
            if json_result:
                raise json_reply(result)
            if xml_result:
                raise Reply(201, b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(result), "application/xml")
            return result
        raise ConflictError()

    @staticmethod
    def next_id(state, kind):
        state["ids"][kind] = state["ids"].get(kind, 0) + 1
        return str(state["ids"][kind])

    @staticmethod
    def note(state, actor, kind, text, **details):
        state["activity"].append({"at": now_iso(), "actor": actor, "kind": kind, "text": text, **details})
        del state["activity"][:-400]

    # Groups and scope

    @staticmethod
    def matches(device, criteria):
        """Smart group criteria, left to right. No criteria: every device."""
        result = True
        for index, criterion in enumerate(criteria):
            value = device.get("name", "") if "Name" in criterion["name"] else device.get("serial", "")
            wanted, search = criterion["value"], criterion["search_type"]
            hit = {"is": value == wanted, "is not": value != wanted,
                   "like": wanted.lower() in value.lower(), "not like": wanted.lower() not in value.lower(),
                   "matches regex": re.search(wanted, value) is not None,
                   "does not match regex": re.search(wanted, value) is None}.get(search, False)
            result = hit if index == 0 else (result and hit if criterion.get("and_or", "and") == "and" else result or hit)
        return result

    def members(self, state, kind, group_id):
        group = state[f"{kind}_groups"].get(str(group_id))
        devices = state["computers" if kind == "computer" else "mobile_devices"]
        if group is None:
            return []
        if not group.get("is_smart"):
            return [d for d in group.get("static", []) if d in devices]
        return [device_id for device_id, device in devices.items() if self.matches(device, group["criteria"])]

    def in_scope(self, state, kind, scope, device_id):
        if scope.get("all"):
            return True
        return any(device_id in self.members(state, kind, g) for g in scope.get("groups", []))

    def group_id(self, state, kind, name):
        for group_id, group in state[f"{kind}_groups"].items():
            if group["name"] == name:
                return group_id
        raise Reply(409, f"No {kind.replace('_', ' ')} group named {name}")

    # Agentless devices

    def simulate(self, state):
        """Simulated devices install what is scoped to them straight away."""
        for computer_id, computer in state["computers"].items():
            if not computer.get("simulated"):
                continue
            for policy_id, policy in state["policies"].items():
                if self.due(state, policy, computer_id):
                    self.run_policy(state, policy_id, computer_id, computer)
        for device_id, device in state["mobile_devices"].items():
            if not device.get("simulated"):
                continue
            for app in state["mobile_apps"].values():
                if app.get("deploy_automatically") and app.get("file") and self.in_scope(state, "mobile_device", app["scope"], device_id):
                    installed = next((a for a in device["applications"] if a["identifier"] == app["bundle_id"]), None)
                    if installed is None or installed["shortVersion"] != app["version"]:
                        device["applications"] = [a for a in device["applications"] if a["identifier"] != app["bundle_id"]]
                        device["applications"].append({"identifier": app["bundle_id"], "name": app["name"],
                                                       "version": app.get("build") or app["version"],
                                                       "shortVersion": app["version"]})
                        device["last_inventory"] = now_iso()
                        self.note(state, device["name"], "install", f"{device['name']} installed {app['name']} {app['version']}",
                                  device=device["name"], app=app["name"], version=app["version"])

    def due(self, state, policy, computer_id):
        """Is a once-per-computer policy, triggered at check-in, still to run on this computer?"""
        return (policy.get("enabled") and policy.get("trigger_checkin") and policy.get("packages")
                and self.in_scope(state, "computer", policy["scope"], computer_id)
                and not any(log["computer"] == computer_id and log["status"] == "Completed" for log in policy["logs"]))

    def run_policy(self, state, policy_id, computer_id, computer):
        policy = state["policies"][policy_id]
        for package_ref in policy["packages"]:
            package = state["packages"].get(str(package_ref["id"]))
            for app in (package or {}).get("contents", []):
                computer["applications"] = [a for a in computer["applications"] if a["bundleId"] != app["bundleId"]]
                computer["applications"].append(dict(app))
        computer["last_contact"] = computer["report_date"] = now_iso()
        policy["logs"].append({"computer": computer_id, "status": "Completed", "at": now_iso()})
        names = ", ".join(state["packages"][str(p["id"])]["packageName"] for p in policy["packages"] if str(p["id"]) in state["packages"])
        self.note(state, computer["name"], "install", f"{computer['name']} ran {policy['name']}: installed {names}",
                  device=computer["name"], policy=policy["name"])

    def add_simulated(self, state, request):
        kind = "computers" if request["kind"] == "computer" else "mobile_devices"
        for device_id, device in state[kind].items():
            if device["name"] == request["name"]:
                if request.get("applications") is not None and device.get("simulated"):
                    device["applications"] = request["applications"]
                    self.note(state, "API client", "device", f"Reset {request['name']}")
                return {"id": device_id, "existing": True}
        device_id = self.next_id(state, kind)
        if kind == "computers":
            state[kind][device_id] = {"name": request["name"], "serial": f"SIM{int(device_id):07d}", "simulated": True,
                                      "applications": request.get("applications", []), "last_contact": now_iso(),
                                      "report_date": now_iso()}
        else:
            state[kind][device_id] = {"name": request["name"], "serial": f"SIM{int(device_id):07d}", "simulated": True,
                                      "applications": request.get("applications", []), "last_inventory": now_iso()}
        self.note(state, "API client", "device", f"Added {request['name']}")
        return {"id": device_id, "existing": False}

    # Jamf Pro API

    def pro_api(self, method, path, query, headers, body):
        if path == "/api/v1/packages" and method == "GET":
            state, _ = self.read()
            wanted = re.search(r'packageName=="(.*)"', query.get("filter", [""])[0])
            results = [self.package_json(i, p) for i, p in sorted(state["packages"].items(), key=lambda x: int(x[0]))
                       if not wanted or p["packageName"] == wanted.group(1)]
            raise json_reply({"totalCount": len(results), "results": results})
        if path == "/api/v1/packages" and method == "POST":
            request = json.loads(body)

            def create(state):
                if any(p["packageName"] == request["packageName"] for p in state["packages"].values()):
                    raise Reply(400, json.dumps({"httpStatus": 400, "errors": [{"code": "DUPLICATE_FIELD", "field": "packageName"}]}),
                                "application/json")
                package_id = self.next_id(state, "packages")
                state["packages"][package_id] = {**request, "hashType": None, "hashValue": None, "contents": [],
                                                 "size": None, "uploadedAt": None}
                self.note(state, "API client", "package", f"Created package {request['packageName']}")
                return {"id": package_id, "href": f"{self.base_url}/api/v1/packages/{package_id}"}

            result = self.mutate(create)
            raise Reply(201, json.dumps(result), "application/json")
        match = re.fullmatch(r"/api/v1/packages/(\d+)(/upload)?", path)
        if match and method == "GET" and not match.group(2):
            state, _ = self.read()
            if match.group(1) not in state["packages"]:
                raise Reply(404, json.dumps({"httpStatus": 404}), "application/json")
            raise json_reply(self.package_json(match.group(1), state["packages"][match.group(1)]))
        if match and method == "POST" and match.group(2):
            filename, data = self.uploaded(headers, body, "file")
            package_id = match.group(1)
            key = f"packages/{package_id}/{filename}"
            self.store.put_file(key, data)

            def upload(state):
                package = state["packages"].get(package_id)
                if package is None:
                    raise Reply(404, json.dumps({"httpStatus": 404}), "application/json")
                package.update({"fileName": filename, "file": key, "size": len(data), "hashType": "SHA_512",
                                "hashValue": hashlib.sha512(data).hexdigest(), "contents": package_contents(data),
                                "uploadedAt": now_iso()})
                self.note(state, "API client", "upload", f"Uploaded {filename} to package {package['packageName']}",
                          package=package["packageName"])
                return {"id": package_id, "href": f"{self.base_url}/api/v1/packages/{package_id}"}

            result = self.mutate(upload)
            raise Reply(201, json.dumps(result), "application/json")
        if path == "/api/v1/computers-inventory" and method == "GET":
            state, _ = self.read()
            results = [self.computer_json(i, c) for i, c in sorted(state["computers"].items(), key=lambda x: int(x[0]))]
            raise json_reply({"totalCount": len(results), "results": results})
        match = re.fullmatch(r"/api/v1/computers-inventory(?:-detail)?/(\d+)", path)
        if match and method == "GET":
            state, _ = self.read()
            computer = state["computers"].get(match.group(1))
            if computer is None:
                raise Reply(404, json.dumps({"httpStatus": 404}), "application/json")
            raise json_reply(self.computer_json(match.group(1), computer))
        if path == "/api/v2/mobile-devices/detail" and method == "GET":
            state, _ = self.read()
            results = [{"mobileDeviceId": i, "general": {"displayName": d["name"], "lastInventoryUpdateDate": d.get("last_inventory")},
                        "hardware": {"serialNumber": d["serial"]}, "applications": d["applications"]}
                       for i, d in sorted(state["mobile_devices"].items(), key=lambda x: int(x[0]))]
            raise json_reply({"totalCount": len(results), "results": results})

    @staticmethod
    def package_json(package_id, package):
        return {"id": package_id, **{k: v for k, v in package.items() if k not in ("file", "contents", "uploadedAt")}}

    @staticmethod
    def computer_json(computer_id, computer):
        return {"id": computer_id, "udid": computer.get("udid"),
                "general": {"name": computer["name"], "lastContactTime": computer.get("last_contact"),
                            "reportDate": computer.get("report_date"), "remoteManagement": {"managed": True}},
                "hardware": {"serialNumber": computer.get("serial")},
                "applications": computer["applications"]}

    def uploaded(self, headers, body, field):
        """The file of a multipart upload, or of a stand-in upload made to S3 first."""
        if headers.get("x-standin-upload"):
            key = headers["x-standin-upload"]
            if not key.startswith("uploads/"):
                raise Reply(400, "Bad upload key")
            return key.rsplit("/", 1)[-1], self.store.get_file(key)
        content_type = headers.get("content-type", "")
        if "multipart/form-data" not in content_type:
            raise Reply(415, "Expected multipart/form-data")
        message = email.message_from_bytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        for part in message.get_payload():
            if part.get_param("name", header="content-disposition") == field:
                return part.get_filename() or "upload", part.get_payload(decode=True)
        raise Reply(400, f"No {field} field in the upload")

    # Classic API

    def classic_api(self, method, path, headers, body):
        parts = path.strip("/").split("/")
        resource = parts[0]
        if resource == "logflush" and method == "DELETE" and len(parts) >= 4 and parts[1] == "policy":
            policy_id = parts[3]

            def flush(state):
                policy = self.classic_find(state, "policies", "id", policy_id)[1]
                policy["logs"] = []
                self.note(state, "API client", "flush", f"Flushed the logs of {policy['name']}: it runs again on every computer in scope",
                          policy=policy["name"])

            self.mutate(flush)
            raise Reply(200, b'<?xml version="1.0" encoding="UTF-8"?><logflush><status>Completed</status></logflush>',
                        "application/xml")
        if resource == "fileuploads" and method == "POST" and len(parts) == 4 and parts[1] == "mobiledeviceapplicationsipa":
            app_id = parts[3]
            filename, data = self.uploaded(headers, body, "name")
            info = ipa_contents(data)
            key = f"ipas/{app_id}/{filename}"
            self.store.put_file(key, data)

            def upload_ipa(state):
                app = self.classic_find(state, "mobile_apps", "id", app_id)[1]
                before = app.get("version")
                app.update({"file": key, "file_name": filename, "version": info["version"], "build": info["build"],
                            "bundle_id": info["bundleId"] or app["bundle_id"], "uploaded_at": now_iso()})
                self.note(state, "API client", "upload", f"Uploaded {filename}: {app['name']} {info['version']} (build {info['build']})",
                          app=app["name"], before=before, after=info["version"])

            self.mutate(upload_ipa)
            raise Reply(201, b"", "application/xml")
        kinds = {"policies": ("policies", "policy"), "computergroups": ("computer_groups", "computer_group"),
                 "mobiledevicegroups": ("mobile_device_groups", "mobile_device_group"),
                 "mobiledeviceapplications": ("mobile_apps", "mobile_device_application")}
        if resource not in kinds or len(parts) < 3:
            return
        table, tag = kinds[resource]
        by, key = parts[1], urllib.parse.unquote(parts[2])
        if method == "GET":
            state, _ = self.read()
            found = self.classic_find(state, table, by, key, missing_ok=True)
            if found is None:
                raise Reply(404, "<html><body>The server has not found anything matching the request URI</body></html>",
                            "text/html")
            raise Reply(200, b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(self.to_xml(state, table, *found)),
                        "application/xml")
        if method in ("POST", "PUT"):
            element = ET.fromstring(body)

            def write(state):
                if method == "POST":
                    item_id = self.next_id(state, table)
                    state[table][item_id] = self.new_item(table, element)
                    item = state[table][item_id]
                    self.note(state, "API client", "create", f"Created {item['name']}")
                else:
                    item_id, item = self.classic_find(state, table, by, key)
                self.apply(state, table, item, element)
                reply = ET.Element(tag)
                sub(reply, "id", item_id)
                return reply

            self.mutate(write, xml_result=True)

    @staticmethod
    def classic_find(state, table, by, key, missing_ok=False):
        for item_id, item in state[table].items():
            if (by == "id" and item_id == str(key)) or (by == "name" and item["name"] == key):
                return item_id, item
        if missing_ok:
            return None
        raise Reply(404, "<html><body>The server has not found anything matching the request URI</body></html>", "text/html")

    @staticmethod
    def new_item(table, element):
        if table == "policies":
            return {"name": "", "enabled": True, "trigger_checkin": False, "frequency": "Once per computer",
                    "packages": [], "scope": {"all": False, "groups": []}, "recon": False, "logs": []}
        if table in ("computer_groups", "mobile_device_groups"):
            return {"name": "", "is_smart": True, "criteria": [], "static": []}
        return {"name": "", "bundle_id": "", "version": "", "internal_app": True, "deploy_automatically": True,
                "scope": {"all": False, "groups": []}}

    def apply(self, state, table, item, element):
        """Apply a Classic API document: the elements it carries replace the item's."""
        kind = "computer" if table in ("policies", "computer_groups") else "mobile_device"
        general = element.find("general") if table in ("policies", "mobile_apps") else element
        if general is not None:
            for field in ("name", "frequency", "bundle_id", "version"):
                if general.findtext(field) is not None:
                    item[field] = general.findtext(field)
            for field in ("enabled", "trigger_checkin", "deploy_automatically", "internal_app", "is_smart"):
                if general.findtext(field) is not None:
                    item[field] = general.findtext(field) == "true"
        if element.find("criteria") is not None:
            item["criteria"] = [{"name": c.findtext("name"), "search_type": c.findtext("search_type"),
                                 "value": c.findtext("value") or "", "and_or": c.findtext("and_or") or "and"}
                                for c in element.findall("criteria/criterion")]
        if element.find("maintenance/recon") is not None:
            item["recon"] = element.findtext("maintenance/recon") == "true"
        packages = element.find("package_configuration/packages")
        if packages is not None:
            before = [state["packages"].get(str(p["id"]), {}).get("packageName") for p in item.get("packages", [])]
            item["packages"] = []
            for package in packages.findall("package"):
                package_id = package.findtext("id") or next(
                    (i for i, p in state["packages"].items() if p["packageName"] == package.findtext("name")), None)
                if package_id not in state["packages"]:
                    raise Reply(409, f"No package {package.findtext('id') or package.findtext('name')}")
                item["packages"].append({"id": int(package_id), "action": package.findtext("action") or "Install"})
            after = [state["packages"][str(p["id"])]["packageName"] for p in item["packages"]]
            if before != after:
                self.note(state, "API client", "package", f"{item['name']}: package {', '.join(after) or 'none'}",
                          policy=item["name"], before=", ".join(b for b in before if b) or None, after=", ".join(after))
        scope = element.find("scope")
        if scope is not None:
            before = dict(item["scope"])
            if scope.findtext(f"all_{kind}s") is not None:
                item["scope"]["all"] = scope.findtext(f"all_{kind}s") == "true"
            groups = scope.find(f"{kind}_groups")
            if groups is not None:
                item["scope"]["groups"] = [g.findtext("id") or self.group_id(state, kind, g.findtext("name"))
                                           for g in groups.findall(f"{kind}_group")]
            if before != item["scope"]:
                names = lambda s: "All" if s.get("all") else ", ".join(
                    state[f"{kind}_groups"][g]["name"] for g in s.get("groups", []) if g in state[f"{kind}_groups"]) or "nobody"
                self.note(state, "API client", "scope", f"{item['name']}: scope {names(before)} to {names(item['scope'])}",
                          policy=item["name"], before=names(before), after=names(item["scope"]))
                item["scope_changed_at"] = now_iso()

    def to_xml(self, state, table, item_id, item):
        if table == "policies":
            policy = ET.Element("policy")
            general = sub(policy, "general")
            for key, value in (("id", item_id), ("name", item["name"]), ("enabled", str(item["enabled"]).lower()),
                               ("trigger", "EVENT"), ("trigger_checkin", str(item["trigger_checkin"]).lower()),
                               ("frequency", item["frequency"])):
                sub(general, key, value)
            policy.append(self.scope_xml(state, "computer", item["scope"]))
            packages = sub(sub(policy, "package_configuration"), "packages")
            sub(packages, "size", len(item["packages"]))
            for ref in item["packages"]:
                package = sub(packages, "package")
                sub(package, "id", ref["id"])
                sub(package, "name", state["packages"].get(str(ref["id"]), {}).get("packageName", ""))
                sub(package, "action", ref["action"])
            sub(sub(policy, "maintenance"), "recon", str(item["recon"]).lower())
            return policy
        if table in ("computer_groups", "mobile_device_groups"):
            kind = "computer" if table == "computer_groups" else "mobile_device"
            group = ET.Element(f"{kind}_group")
            sub(group, "id", item_id)
            sub(group, "name", item["name"])
            sub(group, "is_smart", str(item["is_smart"]).lower())
            criteria = sub(group, "criteria")
            sub(criteria, "size", len(item["criteria"]))
            for priority, c in enumerate(item["criteria"]):
                criterion = sub(criteria, "criterion")
                for key, value in (("name", c["name"]), ("priority", priority), ("and_or", c["and_or"]),
                                   ("search_type", c["search_type"]), ("value", c["value"])):
                    sub(criterion, key, value)
            devices = state["computers" if kind == "computer" else "mobile_devices"]
            members = sub(group, f"{kind}s")
            ids = self.members(state, kind, item_id)
            sub(members, "size", len(ids))
            for device_id in ids:
                member = sub(members, kind)
                sub(member, "id", device_id)
                sub(member, "name", devices[device_id]["name"])
            return group
        app = ET.Element("mobile_device_application")
        general = sub(app, "general")
        for key, value in (("id", item_id), ("name", item["name"]), ("display_name", item["name"]),
                           ("bundle_id", item["bundle_id"]), ("version", item["version"]),
                           ("internal_app", str(item["internal_app"]).lower()),
                           ("deploy_automatically", str(item["deploy_automatically"]).lower())):
            sub(general, key, value)
        app.append(self.scope_xml(state, "mobile_device", item["scope"]))
        return app

    @staticmethod
    def scope_xml(state, kind, scope):
        element = ET.Element("scope")
        sub(element, f"all_{kind}s", str(scope.get("all", False)).lower())
        sub(element, f"{kind}s")
        groups = sub(element, f"{kind}_groups")
        for group_id in scope.get("groups", []):
            group = sub(groups, f"{kind}_group")
            sub(group, "id", group_id)
            sub(group, "name", state[f"{kind}_groups"].get(group_id, {}).get("name", ""))
        exclusions = sub(element, "exclusions")
        sub(exclusions, f"{kind}s")
        sub(exclusions, f"{kind}_groups")
        return element

    # The pilot Mac's agent

    def agent(self, method, path, headers, body):
        if method != "POST":
            raise Reply(405, "POST only")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(errors="replace")).items()}
        if path == "/standin/agent/enroll":
            if not hmac.compare_digest(form.get("code", ""), self.enrollment_code):
                raise Reply(403, "Invalid enrollment invitation")

            def enroll(state):
                existing = next((i for i, c in state["computers"].items()
                                 if c.get("serial") == form.get("serial") and not c.get("simulated")), None)
                computer_id = existing or self.next_id(state, "computers")
                computer = state["computers"].setdefault(computer_id, {"applications": []})
                computer.update({"name": form.get("name", "Mac"), "serial": form.get("serial", ""),
                                 "udid": form.get("udid", ""), "simulated": False, "last_contact": now_iso(),
                                 "report_date": now_iso(), "os": form.get("os", "")})
                self.note(state, computer["name"], "enroll", f"{computer['name']} enrolled")
                return computer_id

            computer_id = self.mutate(enroll)
            raise Reply(200, f"computer_id={computer_id}\ntoken={self.signer.sign('device.' + computer_id, 10 * 365 * 86400)}\n")
        computer_id = form.get("computer_id", "")
        if not self.signer.check("device." + computer_id, headers.get("authorization", "").removeprefix("Bearer ")):
            raise Reply(401, "Unknown device: enroll again")
        if path == "/standin/agent/checkin":
            def checkin(state):
                computer = state["computers"][computer_id]
                computer["last_contact"] = now_iso()
                lines = []
                for policy_id, policy in sorted(state["policies"].items(), key=lambda x: int(x[0])):
                    if self.due(state, policy, computer_id):
                        for ref in policy["packages"]:
                            package = state["packages"].get(str(ref["id"]))
                            if package and package.get("file"):
                                url = self.store.download_url(package["file"], self.signer)
                                lines.append("\t".join(["policy", policy_id, policy["name"], package["fileName"],
                                                        package["hashValue"], "1" if policy["recon"] else "0", url]))
                return "\n".join(lines) + "\n"

            raise Reply(200, self.mutate(checkin))
        if path == "/standin/agent/log":
            def policy_log(state):
                policy = state["policies"].get(form.get("policy_id", ""))
                if policy is not None:
                    policy["logs"].append({"computer": computer_id, "status": form.get("status", "Completed"),
                                           "at": now_iso(), "details": form.get("details", "")[:500]})
                    name = state["computers"][computer_id]["name"]
                    self.note(state, name, "install", f"{name} ran {policy['name']}: {form.get('status')}",
                              device=name, policy=policy["name"])

            self.mutate(policy_log)
            raise Reply(200, "ok\n")
        if path == "/standin/agent/inventory":
            applications = []
            for line in form.get("applications", "").splitlines():
                fields = line.split("\t")
                if len(fields) == 4:
                    applications.append({"name": fields[0], "version": fields[1], "bundleId": fields[2], "path": fields[3]})

            def inventory(state):
                computer = state["computers"][computer_id]
                computer.update({"applications": applications, "report_date": now_iso(), "last_contact": now_iso(),
                                 "os": form.get("os", computer.get("os", ""))})
                self.note(state, computer["name"], "inventory", f"{computer['name']} submitted inventory", device=computer["name"])

            self.mutate(inventory)
            raise Reply(200, f"<computer_id>{computer_id}</computer_id>\n")

    def files(self, method, path, query, body):
        key = urllib.parse.unquote(path[len("/standin/files/"):])
        signature = query.get("sig", [""])[0]
        if method == "GET" and self.signer.check("file." + key, signature):
            raise Reply(200, self.store.get_file(key), "application/octet-stream")
        if method == "PUT" and key.startswith("uploads/") and self.signer.check("upload." + key, signature):
            self.store.put_file(key, body)
            raise Reply(200, "")
        raise Reply(403, "Link expired or invalid")

    # The page

    def ui(self, path, query, headers):
        if path == "/standin/ui/login":
            if not self.signer.check("ui", query.get("token", [""])[0]):
                raise Reply(403, "This link has expired. Make a new one: python scripts/demos/standins/jamf.py ui-link")
            cookie = f"standin_ui={self.signer.sign('ui', UI_SECONDS)}; Path=/standin/ui; HttpOnly; Secure; SameSite=Lax"
            target = query.get("next", ["/standin/ui"])[0]
            raise Reply(302, "", headers={"Location": target if target.startswith("/standin/ui") else "/standin/ui",
                                          "Set-Cookie": cookie})
        cookies = dict(c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c)
        if not self.signer.check("ui", cookies.get("standin_ui", "")):
            raise Reply(401, "Sign in with a link from: python scripts/demos/standins/jamf.py ui-link")
        if path == "/standin/ui/state.json":
            state, _ = self.read()
            raise json_reply(self.view(state))
        raise Reply(200, PAGE, "text/html; charset=utf-8")

    def view(self, state):
        """What the page shows: each policy and app with its scope, and its groups' inventory."""
        def group_rows(kind, group_ids, bundle_id):
            rows = []
            devices = state["computers" if kind == "computer" else "mobile_devices"]
            for group_id in group_ids:
                group = state[f"{kind}_groups"].get(group_id, {})
                members = []
                for device_id in self.members(state, kind, group_id):
                    device = devices[device_id]
                    app = next((a for a in device["applications"]
                                if a.get("bundleId", a.get("identifier")) == bundle_id), None)
                    members.append({"name": device["name"], "simulated": device.get("simulated", False),
                                    "version": (app.get("version") if kind == "computer" else app.get("shortVersion")) if app else None,
                                    "reported": device.get("report_date") or device.get("last_inventory")})
                rows.append({"group": group.get("name"), "members": members})
            return rows

        def bundle_of(policy):
            for ref in policy["packages"]:
                for app in state["packages"].get(str(ref["id"]), {}).get("contents", []):
                    return app["bundleId"], app.get("version")
            return None, None

        policies = []
        for policy_id, policy in sorted(state["policies"].items(), key=lambda x: int(x[0])):
            bundle_id, version = bundle_of(policy)
            scope_groups = list(policy["scope"]["groups"])
            all_groups = [g for g in state["computer_groups"] if g not in scope_groups]
            policies.append({
                "kind": "policy", "name": policy["name"], "enabled": policy["enabled"],
                "packages": [state["packages"].get(str(p["id"]), {}).get("packageName") for p in policy["packages"]],
                "version": version,
                "scope": "All computers" if policy["scope"]["all"] else
                         [state["computer_groups"].get(g, {}).get("name") for g in scope_groups],
                "scope_changed_at": policy.get("scope_changed_at"),
                "trigger": "Recurring check-in" if policy["trigger_checkin"] else "None",
                "frequency": policy["frequency"], "recon": policy["recon"],
                "completed": sum(1 for log in policy["logs"] if log["status"] == "Completed"),
                "inventory": group_rows("computer", scope_groups + all_groups, bundle_id),
            })
        apps = []
        for app_id, app in sorted(state["mobile_apps"].items(), key=lambda x: int(x[0])):
            scope_groups = list(app["scope"]["groups"])
            all_groups = [g for g in state["mobile_device_groups"] if g not in scope_groups]
            apps.append({
                "kind": "app", "name": app["name"], "version": app.get("version"), "build": app.get("build"),
                "packages": [app.get("file_name")] if app.get("file_name") else [],
                "scope": "All mobile devices" if app["scope"]["all"] else
                         [state["mobile_device_groups"].get(g, {}).get("name") for g in scope_groups],
                "scope_changed_at": app.get("scope_changed_at"),
                "inventory": group_rows("mobile_device", scope_groups + all_groups, app.get("bundle_id")),
            })
        return {"now": now_iso(), "items": policies + apps, "activity": state["activity"][-12:][::-1]}


class ConflictError(Exception):
    pass


def json_reply(data, status=200):
    return Reply(status, json.dumps(data), "application/json")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Device management</title>
<style>
:root { --bg:#f5f6f8; --card:#fff; --ink:#1d2330; --muted:#677084; --line:#dde1e8; --accent:#2457d6; --ok:#1e7d4f; --warn:#a65d00; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; background:var(--bg); color:var(--ink); }
header { background:#1f2a44; color:#fff; padding:14px 24px; font-size:18px; font-weight:600; display:flex; justify-content:space-between; }
header span { font-weight:400; opacity:.75; font-size:14px; }
main { max-width:1100px; margin:0 auto; padding:20px 16px 40px; display:grid; gap:18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; }
h2 { margin:0 0 4px; font-size:22px; }
.facts { display:grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap:10px 18px; margin:14px 0; }
.fact b { display:block; color:var(--muted); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.fact span { font-size:17px; }
.version { font-size:30px; font-weight:700; }
.chip { display:inline-block; padding:3px 10px; border-radius:999px; background:#e8eefc; color:var(--accent); font-weight:600; margin:2px 4px 2px 0; }
.chip.changed { outline:3px solid #f2b705; outline-offset:2px; }
table { width:100%; border-collapse:collapse; margin-top:6px; }
th, td { text-align:left; padding:7px 8px; border-top:1px solid var(--line); }
th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
td.v { font-weight:700; font-variant-numeric: tabular-nums; }
.group { margin-top:12px; font-weight:600; }
.muted { color:var(--muted); }
.activity li { margin:4px 0; }
@media (max-width: 600px) { header { flex-direction:column; gap:4px; } .version { font-size:24px; } }
</style></head>
<body><header>Device management <span id="clock"></span></header><main id="main"><div class="card muted">Loading</div></main>
<script>
const only = new URLSearchParams(location.search).get("only");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const time = iso => iso ? new Date(iso).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "never";
function card(item, now) {
  const recent = item.scope_changed_at && (new Date(now) - new Date(item.scope_changed_at)) < 60000;
  const scope = Array.isArray(item.scope) ? (item.scope.length ? item.scope : ["Nobody"]) : [item.scope];
  const facts = item.kind === "policy"
    ? [["Package", item.packages.join(", ") || "None"], ["Trigger", item.trigger], ["Frequency", item.frequency],
       ["Inventory update", item.recon ? "After the policy runs" : "No"], ["Completed", item.completed + " computers"]]
    : [["App file", item.packages.join(", ") || "None"], ["Build", item.build || ""], ["Distribution", "Install automatically"]];
  const groups = item.inventory.map(g => `<div class="group">${esc(g.group)} <span class="muted">(${g.members.length})</span></div>
    <table><tr><th>${item.kind === "policy" ? "Computer" : "Device"}</th><th>Version</th><th>Last report</th></tr>
    ${g.members.map(m => `<tr><td>${esc(m.name)}</td><td class="v">${esc(m.version || "not installed")}</td><td class="muted">${time(m.reported)}</td></tr>`).join("") || '<tr><td class="muted" colspan="3">No members</td></tr>'}</table>`).join("");
  return `<section class="card"><div class="muted">${item.kind === "policy" ? "Policy" : "Mobile device app"}</div>
    <h2>${esc(item.name)}</h2><div class="version">${esc(item.version || "")}</div>
    <div class="facts"><div class="fact"><b>Scope</b>${scope.map(s => `<span class="chip${recent ? " changed" : ""}">${esc(s)}</span>`).join("")}</div>
    ${facts.map(([k, v]) => `<div class="fact"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("")}</div>${groups}</section>`;
}
async function refresh() {
  try {
    const response = await fetch("/standin/ui/state.json", {cache: "no-store"});
    if (!response.ok) { document.getElementById("main").innerHTML = `<div class="card">${esc(await response.text())}</div>`; return; }
    const view = await response.json();
    const items = view.items.filter(i => !only || i.name === only);
    document.getElementById("clock").textContent = time(view.now);
    document.getElementById("main").innerHTML = items.map(i => card(i, view.now)).join("") +
      (only ? "" : `<section class="card"><h2>Activity</h2><ul class="activity">${view.activity.map(a =>
        `<li><span class="muted">${time(a.at)}</span> ${esc(a.text)}</li>`).join("")}</ul></section>`);
  } catch (error) { console.error(error); }
}
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


# Lambda and the local server

_STANDIN = None


def lambda_handler(event, context):
    """The stand-in behind a Lambda function URL. Secrets come from SSM once per cold start."""
    global _STANDIN
    if _STANDIN is None:
        import boto3
        ssm = boto3.client("ssm")
        prefix = os.environ["PARAMETER_PREFIX"]
        values = {}
        for name in ("client-id", "client-secret", "signing-key", "enrollment-code"):
            values[name] = ssm.get_parameter(Name=prefix + name, WithDecryption=True)["Parameter"]["Value"]
        _STANDIN = Standin(S3Store(os.environ["BUCKET"]), values["client-id"], values["client-secret"],
                           values["signing-key"], values["enrollment-code"])
    http = event["requestContext"]["http"]
    body = event.get("body") or ""
    body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = dict(event.get("headers") or {})
    if event.get("cookies"):
        headers["cookie"] = "; ".join(event["cookies"])
    base_url = f"https://{event['requestContext']['domainName']}"
    status, content_type, extra, payload = _STANDIN.handle(
        http["method"], event.get("rawPath", "/"), urllib.parse.parse_qs(event.get("rawQueryString", "")),
        headers, body, base_url)
    response = {"statusCode": status, "headers": {"Content-Type": content_type, "Cache-Control": "no-store", **extra}}
    if "Set-Cookie" in response["headers"]:
        response["cookies"] = [response["headers"].pop("Set-Cookie")]
    text = content_type.startswith(("text/", "application/json", "application/xml"))
    response["body"] = payload.decode() if text else base64.b64encode(payload).decode()
    response["isBase64Encoded"] = not text
    return response


def make_server(directory, port, client_id, client_secret, signing_key, enrollment_code, host="0.0.0.0"):
    """The stand-in as an HTTP server on this machine, for development and the tests."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    lock = threading.Lock()
    standin = Standin(LocalStore(directory), client_id, client_secret, signing_key, enrollment_code)

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            url = urllib.parse.urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with lock:
                status, content_type, extra, payload = standin.handle(
                    self.command, url.path, urllib.parse.parse_qs(url.query), dict(self.headers), body,
                    f"http://{self.headers.get('Host', f'127.0.0.1:{port}')}")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in extra.items():
                self.send_header(key, value.replace("; Secure", ""))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = respond

        def log_message(self, fmt, *args):
            if os.environ.get("JAMF_STANDIN_QUIET") != "1":
                print(f"{self.address_string()} {fmt % args}", flush=True)

    return ThreadingHTTPServer((host, port), Handler)


def serve(directory, port, client_id, client_secret, signing_key, enrollment_code):
    server = make_server(directory, port, client_id, client_secret, signing_key, enrollment_code)
    print(f"Jamf Pro stand-in on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub_parsers = parser.add_subparsers(dest="command", required=True)
    server = sub_parsers.add_parser("serve", help="run the stand-in on this machine")
    server.add_argument("--dir", default=".local/jamf-standin")
    server.add_argument("--port", type=int, default=8090)
    sub_parsers.add_parser("ui-link", help="a signed link to the stand-in's page, valid 12 hours")
    status = sub_parsers.add_parser("status", help="a policy or app: its package or version, scope and inventory")
    status.add_argument("--policy", help="a policy name, such as Store Assistant")
    status.add_argument("--app", help="a mobile device app name, such as Store Floor")
    status.add_argument("--bundle-id", required=True)
    status.add_argument("--groups", required=True, help="comma-separated groups to list, such as Pilot,All managed Macs")
    args = parser.parse_args()

    if args.command == "serve":
        values = {name: os.environ.get(f"JAMF_STANDIN_{name.upper().replace('-', '_')}") or secrets.token_hex(16)
                  for name in ("client-id", "client-secret", "signing-key", "enrollment-code")}
        serve(args.dir, args.port, values["client-id"], values["client-secret"], values["signing-key"],
              values["enrollment-code"])
        return

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common import get_parameter, ssm_client
    ssm = ssm_client()
    if args.command == "ui-link":
        url = get_parameter(ssm, "jamf/standin-url").rstrip("/")
        token = Signer(get_parameter(ssm, "jamf/standin-signing-key")).sign("ui", UI_SECONDS)
        print(f"{url}/standin/ui/login?token={token}")
        return
    jamf = connect(ssm=ssm)
    print_status(jamf, args)


def print_status(jamf, args):
    """What the page shows, as text: for a terminal, a log or a recording."""
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    if args.policy:
        policy = jamf.policy(args.policy)
        print(f"Policy {policy['name']}: {', '.join(p['name'] for p in policy['packages']) or 'no package'}")
        scope = "All computers" if policy["scope"]["all"] else ", ".join(policy["scope"]["groups"]) or "nobody"
        versions = lambda group: jamf.app_versions(group, args.bundle_id)
    else:
        app = jamf.mobile_app(args.app)
        print(f"Mobile device app {app['name']}: version {app['version']}")
        scope = "All mobile devices" if app["scope"]["all"] else ", ".join(app["scope"]["groups"]) or "nobody"
        versions = lambda group: jamf.mobile_app_versions(group, args.bundle_id)
    print(f"Scope: {scope}")
    for group in groups:
        rows = versions(group)
        print(f"{group} ({len(rows)})")
        for row in rows:
            print(f"  {row['name']:<24} {row['version'] or 'not installed'}")


if __name__ == "__main__":
    main()
