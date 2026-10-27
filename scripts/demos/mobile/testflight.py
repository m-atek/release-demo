"""TestFlight through the App Store Connect API, or its stand-in.

Both answer the same calls:

    upload(name, data, version, build)       POST /v1/buildUploads, POST /v1/buildUploadFiles,
                                             PUT the parts, PATCH /v1/buildUploadFiles/{id}
    build(version, build)                    GET  /v1/builds with its beta details, groups and notes
    release_to_group(build_id, group, notes) POST /v1/betaBuildLocalizations (What to Test),
                                             POST /v1/betaGroups/{id}/relationships/builds
    expire(build_id)                         PATCH /v1/builds/{id} expired
    builds()                                 GET  /v1/builds, newest first

The App Store Connect API key (an App Manager team key) comes from SSM. Its
JWT is signed with openssl, the key passed through a pipe, never a file.
"""

import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.parse

import mobile
from jamf_apps import build_key
from standin import ServiceError, Standin, http, json_or_none

API = "https://api.appstoreconnect.apple.com"


def testflight(settings=None, store=None):
    config = mobile.config()
    if "apple" in mobile.standins(settings):
        return TestFlightStandin(store or mobile.state_store())
    ssm = mobile.ssm_client()
    key_id, issuer, key = (mobile.get_parameter(ssm, f"{mobile.SERVICE}/{name}")
                           for name in ("asc-key-id", "asc-issuer-id", "asc-private-key"))
    return AppStoreConnect(key_id, issuer, key, config["app"]["bundle_id"])


def der_to_jose(signature):
    """openssl's DER ECDSA signature as the 64 bytes r||s that ES256 wants."""
    def integer(data, position):
        assert data[position] == 0x02
        length = data[position + 1]
        return data[position + 2:position + 2 + length], position + 2 + length
    position = 2 if signature[1] < 0x80 else 3
    r, position = integer(signature, position)
    s, _ = integer(signature, position)
    return r.lstrip(b"\0").rjust(32, b"\0") + s.lstrip(b"\0").rjust(32, b"\0")


def es256_jwt(key_id, issuer, private_key, now=None):
    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    now = int(now or time.time())
    signing_input = (encode(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}).encode()) + b"." +
                     encode(json.dumps({"iss": issuer, "iat": now, "exp": now + 1200,
                                        "aud": "appstoreconnect-v1"}).encode()))
    read_end, write_end = os.pipe()
    os.write(write_end, private_key.encode())
    os.close(write_end)
    try:
        der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_end}"], input=signing_input,
                             capture_output=True, pass_fds=(read_end,), check=True).stdout
    finally:
        os.close(read_end)
    return (signing_input + b"." + encode(der_to_jose(der))).decode()


class AppStoreConnect:
    def __init__(self, key_id, issuer, private_key, bundle_id):
        self.token = es256_jwt(key_id, issuer, private_key)
        self.bundle_id = bundle_id
        self.app = self.call("GET", f"/v1/apps?filter[bundleId]={bundle_id}")["data"][0]["id"]

    def call(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        _, raw = http(method, API + path, headers, body)
        return json_or_none(raw)

    def upload(self, filename, data, version, build):
        upload = self.call("POST", "/v1/buildUploads", {"data": {
            "type": "buildUploads",
            "attributes": {"cfBundleShortVersionString": version, "cfBundleVersion": str(build), "platform": "IOS"},
            "relationships": {"app": {"data": {"type": "apps", "id": self.app}}}}})["data"]
        file = self.call("POST", "/v1/buildUploadFiles", {"data": {
            "type": "buildUploadFiles",
            "attributes": {"assetType": "ASSET", "fileName": filename, "fileSize": len(data), "uti": "com.apple.ipa"},
            "relationships": {"buildUpload": {"data": {"type": "buildUploads", "id": upload["id"]}}}}})["data"]
        for part in file["attributes"]["uploadOperations"]:
            headers = {h["name"]: h["value"] for h in part.get("requestHeaders", [])}
            http(part["method"], part["url"], headers, data[part["offset"]:part["offset"] + part["length"]])
        self.call("PATCH", f"/v1/buildUploadFiles/{file['id']}", {"data": {
            "type": "buildUploadFiles", "id": file["id"],
            "attributes": {"uploaded": True, "sourceFileChecksums": {
                "file": {"hash": hashlib.md5(data).hexdigest(), "algorithm": "MD5"}}}}})
        return {"id": upload["id"]}

    def _group(self, name):
        groups = self.call("GET", f"/v1/betaGroups?filter[app]={self.app}&filter[name]={urllib.parse.quote(name)}")
        if not groups["data"]:
            raise ServiceError(404, f"No beta group {name} on the app")
        return groups["data"][0]["id"]

    def build(self, version, build, group=None):
        query = urllib.parse.urlencode({"filter[app]": self.app, "filter[version]": str(build),
                                        "filter[preReleaseVersion.version]": version, "include": "buildBetaDetail"})
        reply = self.call("GET", f"/v1/builds?{query}")
        if not reply["data"]:
            return None
        found = reply["data"][0]
        detail = next((i for i in reply.get("included", []) if i["type"] == "buildBetaDetails"), {})
        notes = self.call("GET", f"/v1/builds/{found['id']}/betaBuildLocalizations")["data"]
        groups = []
        if group:
            members = self.call("GET", f"/v1/betaGroups/{self._group(group)}/builds?limit=200")["data"]
            groups = [group] if any(b["id"] == found["id"] for b in members) else []
        return {"id": found["id"], "version": version, "build": str(build),
                "processing": found["attributes"]["processingState"],
                "expired": found["attributes"].get("expired", False),
                "internal_state": detail.get("attributes", {}).get("internalBuildState"),
                "groups": groups, "whats_new": notes[0]["attributes"].get("whatsNew") if notes else None}

    def release_to_group(self, build_id, group, notes):
        existing = self.call("GET", f"/v1/builds/{build_id}/betaBuildLocalizations")["data"]
        localized = next((n for n in existing if n["attributes"]["locale"] == "en-US"), None)
        if localized:
            self.call("PATCH", f"/v1/betaBuildLocalizations/{localized['id']}", {"data": {
                "type": "betaBuildLocalizations", "id": localized["id"], "attributes": {"whatsNew": notes}}})
        else:
            self.call("POST", "/v1/betaBuildLocalizations", {"data": {
                "type": "betaBuildLocalizations", "attributes": {"locale": "en-US", "whatsNew": notes},
                "relationships": {"build": {"data": {"type": "builds", "id": build_id}}}}})
        self.call("POST", f"/v1/betaGroups/{self._group(group)}/relationships/builds",
                  {"data": [{"type": "builds", "id": build_id}]})

    def expire(self, build_id):
        self.call("PATCH", f"/v1/builds/{build_id}",
                  {"data": {"type": "builds", "id": build_id, "attributes": {"expired": True}}})

    def builds(self, limit=10):
        reply = self.call("GET", f"/v1/builds?filter[app]={self.app}&sort=-uploadedDate&limit={limit}"
                                 "&include=preReleaseVersion")
        versions = {i["id"]: i["attributes"]["version"] for i in reply.get("included", [])
                    if i["type"] == "preReleaseVersions"}
        return [{"id": b["id"], "build": b["attributes"]["version"],
                 "version": versions.get(b["relationships"]["preReleaseVersion"]["data"]["id"]),
                 "processing": b["attributes"]["processingState"], "expired": b["attributes"].get("expired")}
                for b in reply["data"]]


def initial_state(app_name, bundle_id, group):
    return {"app": {"id": "6740000001", "name": app_name, "bundle_id": bundle_id},
            "groups": {group: {"internal": True, "testers": ["Mateusz Ulas"]}},
            "builds": [], "requests": []}


class TestFlightStandin(Standin):
    key, name, switch = "testflight.json", "TestFlight", "apple"

    def upload(self, filename, data, version, build):
        state = self.load()
        request = "POST /v1/buildUploads"
        try:
            info = mobile.ipa_info(data)
        except (ValueError, KeyError) as error:
            self.record(state, request, "409")
            self.save(state)
            raise ServiceError(409, f"ITMS-90171: Invalid bundle structure: {error}") from None
        problem = None
        same_train = [b for b in state["builds"] if b["version"] == info["version"]]
        if info["bundle_id"] != state["app"]["bundle_id"]:
            problem = (f"ITMS-90055: This bundle is invalid. The bundle identifier {info['bundle_id']} doesn't "
                       f"match the app's, {state['app']['bundle_id']}.")
        elif (info["version"], info["build"]) != (version, str(build)):
            problem = f"The upload says {version} ({build}), but the IPA's Info.plist says {info['version']} ({info['build']})."
        elif not info["signed"]:
            problem = "ITMS-90035: Invalid Signature. The app isn't signed."
        elif info["method"] != "app-store-connect":
            problem = ("ITMS-90161: Invalid Provisioning Profile. The provisioning profile in the bundle "
                       f"is for {info['method']} distribution, not the App Store.")
        elif any(b["build"] == info["build"] for b in same_train):
            problem = (f"ITMS-4238: Redundant Binary Upload. You've already uploaded a build with build number "
                       f"'{info['build']}' for version number '{info['version']}'.")
        elif same_train and build_key(info["build"]) < max(build_key(b["build"]) for b in same_train):
            problem = (f"ITMS-90062: The value for key CFBundleVersion [{info['build']}] must contain a higher "
                       f"version than that of the previously uploaded version.")
        if problem:
            self.record(state, request, "409")
            self.save(state)
            raise ServiceError(409, problem)
        build_id = f"b{len(state['builds']) + 1:04d}"
        state["builds"].insert(0, {
            "id": build_id, "version": info["version"], "build": info["build"], "uploaded_at": self.clock(),
            "file": {"name": filename, "sha256": mobile.sha256(data), "size_bytes": len(data)},
            "processing": "VALID", "expired": False,
            # Without ITSAppUsesNonExemptEncryption, TestFlight waits for the export compliance answers.
            "internal_state": "READY_FOR_BETA_TESTING" if info["encryption_declared"] else "MISSING_EXPORT_COMPLIANCE",
            "groups": [], "whats_new": None})
        for line in ("POST /v1/buildUploads", "POST /v1/buildUploadFiles", "PUT (upload parts)",
                     f"PATCH /v1/buildUploadFiles/{build_id}"):
            self.record(state, line, "201")
        self.save(state)
        return {"id": build_id}

    def _find(self, state, version, build):
        return next((b for b in state["builds"] if b["version"] == version and b["build"] == str(build)), None)

    def build(self, version, build, group=None):
        state = self.load()
        self.record(state, f"GET /v1/builds?filter[version]={build}&filter[preReleaseVersion.version]={version}")
        self.save(state)
        found = self._find(state, version, build)
        return {k: v for k, v in found.items() if k != "file"} if found else None

    def release_to_group(self, build_id, group, notes):
        state = self.load()
        found = next((b for b in state["builds"] if b["id"] == build_id), None)
        if not found or group not in state["groups"]:
            self.record(state, f"POST /v1/betaGroups/{group}/relationships/builds", "404")
            self.save(state)
            raise ServiceError(404, f"There is no build {build_id} or beta group {group}.")
        found["whats_new"] = notes[:4000]
        self.record(state, "POST /v1/betaBuildLocalizations", "201")
        if found["internal_state"] == "MISSING_EXPORT_COMPLIANCE":
            self.record(state, f"POST /v1/betaGroups/{group}/relationships/builds", "409")
            self.save(state)
            raise ServiceError(409, "The build is missing export compliance information.")
        found["groups"] = sorted(set(found["groups"]) | {group})
        found["internal_state"] = "IN_BETA_TESTING"
        self.record(state, f"POST /v1/betaGroups/{group}/relationships/builds", "204")
        self.save(state)

    def expire(self, build_id):
        state = self.load()
        for found in state["builds"]:
            if found["id"] == build_id:
                found["expired"], found["internal_state"] = True, "EXPIRED"
        self.record(state, f"PATCH /v1/builds/{build_id}", "200")
        self.save(state)

    def builds(self, limit=10):
        state = self.load()
        return [{k: v for k, v in b.items() if k != "file"} for b in state["builds"][:limit]]
