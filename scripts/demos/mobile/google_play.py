"""Google Play's testing tracks through the Play Developer API, or its stand-in.

Both answer the same calls, each inside an edit that is committed at the end:

    upload_and_release(track, name, data, status, notes, release_name)
        POST edits, POST edits/{id}/bundles (upload), PUT edits/{id}/tracks/{track}, POST edits/{id}:commit
    set_release(track, version_code, status, notes, release_name)
        POST edits, PUT edits/{id}/tracks/{track}, POST edits/{id}:commit
    track(track)
        POST edits, GET edits/{id}/tracks/{track}, DELETE edits/{id}

The bundle is signed with the upload key; Google keeps the app signing key
(Play App Signing) and checks every upload against the upload certificate.
The API is reached as a service account invited in the Play Console: through
demo 5's Workload Identity Federation when the run has a Jenkins ID token for
Google, otherwise with the service account's JSON key from SSM.
"""

import base64
import json
import os
import subprocess
import time
import urllib.parse

import mobile
from standin import ServiceError, Standin, http, json_or_none

API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"
TARGET_SDK = 36


def google_play(settings=None, store=None):
    config = mobile.config()
    if "google-play" in mobile.standins(settings):
        return GooglePlayStandin(store or mobile.state_store())
    return GooglePlay(config["app"]["package_name"], access_token())


def access_token():
    """Workload Identity Federation with the run's Jenkins ID token, or the JSON key."""
    ssm = mobile.ssm_client()
    if os.environ.get("GOOGLE_ID_TOKEN"):
        federation = json.loads(mobile.get_parameter(ssm, f"{mobile.SERVICE}/play-federation"))
        _, raw = http("POST", "https://sts.googleapis.com/v1/token", {"Content-Type": "application/json"}, {
            "audience": federation["audience"], "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
            "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token", "scope": SCOPE,
            "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt", "subjectToken": os.environ["GOOGLE_ID_TOKEN"]})
        federated = json.loads(raw)["access_token"]
        _, raw = http("POST", "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                              f"{federation['service_account']}:generateAccessToken",
                      {"Authorization": f"Bearer {federated}", "Content-Type": "application/json"},
                      {"scope": [SCOPE], "lifetime": "1800s"})
        return json.loads(raw)["accessToken"]
    key = json.loads(mobile.get_parameter(ssm, f"{mobile.SERVICE}/play-service-account-json"))
    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    now = int(time.time())
    signing_input = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + encode(json.dumps({
        "iss": key["client_email"], "scope": SCOPE, "aud": key["token_uri"], "iat": now, "exp": now + 1800}).encode())
    read_end, write_end = os.pipe()
    os.write(write_end, key["private_key"].encode())
    os.close(write_end)
    try:
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_end}"], input=signing_input,
                                   capture_output=True, pass_fds=(read_end,), check=True).stdout
    finally:
        os.close(read_end)
    form = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": (signing_input + b"." + encode(signature)).decode()}).encode()
    _, raw = http("POST", key["token_uri"], {"Content-Type": "application/x-www-form-urlencoded"}, form)
    return json.loads(raw)["access_token"]


def release_body(track, version_codes, status, notes, release_name):
    release = {"name": release_name, "versionCodes": [str(code) for code in version_codes], "status": status}
    if notes:
        release["releaseNotes"] = [{"language": "en-US", "text": notes[:500]}]
    return {"track": track, "releases": [release]}


class GooglePlay:
    def __init__(self, package, token):
        self.package, self.token = package, token

    def call(self, method, url, body=None, content_type="application/json"):
        _, raw = http(method, url, {"Authorization": f"Bearer {self.token}", "Content-Type": content_type}, body)
        return json_or_none(raw)

    def _edit(self):
        return self.call("POST", f"{API}/{self.package}/edits", {})["id"]

    def upload_and_release(self, track, filename, data, status, notes, release_name):
        edit = self._edit()
        bundle = self.call("POST", f"{UPLOAD}/{self.package}/edits/{edit}/bundles?uploadType=media", data,
                           "application/octet-stream")
        self.call("PUT", f"{API}/{self.package}/edits/{edit}/tracks/{track}",
                  release_body(track, [bundle["versionCode"]], status, notes, release_name))
        self.call("POST", f"{API}/{self.package}/edits/{edit}:commit")
        return int(bundle["versionCode"])

    def set_release(self, track, version_code, status, notes, release_name):
        edit = self._edit()
        self.call("PUT", f"{API}/{self.package}/edits/{edit}/tracks/{track}",
                  release_body(track, [version_code], status, notes, release_name))
        self.call("POST", f"{API}/{self.package}/edits/{edit}:commit")

    def track(self, track):
        edit = self._edit()
        try:
            return self.call("GET", f"{API}/{self.package}/edits/{edit}/tracks/{track}")
        finally:
            self.call("DELETE", f"{API}/{self.package}/edits/{edit}")

    def bundles(self):
        edit = self._edit()
        try:
            return self.call("GET", f"{API}/{self.package}/edits/{edit}/bundles").get("bundles", [])
        finally:
            self.call("DELETE", f"{API}/{self.package}/edits/{edit}")


def initial_state(app_name, package, track):
    return {"app": {"name": app_name, "package": package, "upload_certificate_sha256": None},
            "bundles": [], "tracks": {track: {"track": track, "releases": []}}, "requests": []}


class GooglePlayStandin(Standin):
    key, name, switch = "google-play.json", "Google Play", "google-play"

    def _edit(self, state):
        state["next_edit"] = state.get("next_edit", 1000) + 1
        return str(state["next_edit"])

    def _refuse(self, state, request, status, message):
        self.record(state, request, str(status))
        self.save(state)
        raise ServiceError(status, message)

    def upload_and_release(self, track, filename, data, status, notes, release_name):
        state = self.load()
        package = state["app"]["package"]
        edit = self._edit(state)
        self.record(state, f"POST /applications/{package}/edits", "200")
        request = f"POST /upload/applications/{package}/edits/{edit}/bundles"
        try:
            info = mobile.aab_info(data)
        except (ValueError, KeyError) as error:
            self._refuse(state, request, 400, f"The Android App Bundle is invalid: {error}")
        registered = state["app"].get("upload_certificate_sha256")
        if info["package"] != package:
            self._refuse(state, request, 403, f"APKs and bundles for {package} only: this one is {info['package']}.")
        if not info["signed"]:
            self._refuse(state, request, 400, "The Android App Bundle was not signed.")
        if registered and info["upload_certificate_sha256"] != registered:
            self._refuse(state, request, 400, "Your Android App Bundle is signed with the wrong key. Ensure that your "
                         f"App Bundle is signed with the correct signing key and try again. Expected SHA-256 {registered}, "
                         f"got {info['upload_certificate_sha256']}.")
        if (info["target_sdk"] or 0) < TARGET_SDK:
            self._refuse(state, request, 400, f"Your app currently targets API level {info['target_sdk']} and must "
                         f"target at least API level {TARGET_SDK}.")
        if any(b["version_code"] == info["version_code"] for b in state["bundles"]):
            self._refuse(state, request, 403, f"Version code {info['version_code']} has already been used.")
        if not registered:
            # Play App Signing: the first upload's certificate becomes the upload key.
            state["app"]["upload_certificate_sha256"] = info["upload_certificate_sha256"]
        state["bundles"].insert(0, {"version_code": info["version_code"], "version_name": info["version_name"],
                                    "target_sdk": info["target_sdk"], "sha256": mobile.sha256(data),
                                    "size_bytes": len(data), "uploaded_at": self.clock(), "file": filename})
        self.record(state, request, "200")
        self._set_track(state, edit, track, info["version_code"], status, notes, release_name)
        return info["version_code"]

    def _set_track(self, state, edit, track, version_code, status, notes, release_name):
        package = state["app"]["package"]
        request = f"PUT /applications/{package}/edits/{edit}/tracks/{track}"
        if not any(b["version_code"] == int(version_code) for b in state["bundles"]):
            self._refuse(state, request, 400, f"Version code {version_code} doesn't exist in this app's bundles.")
        body = release_body(track, [version_code], status, notes, release_name)
        body["releases"][0]["updated_at"] = self.clock()
        state["tracks"][track] = body
        self.record(state, request, "200")
        self.record(state, f"POST /applications/{package}/edits/{edit}:commit", "200")
        self.save(state)

    def set_release(self, track, version_code, status, notes, release_name):
        state = self.load()
        edit = self._edit(state)
        self.record(state, f"POST /applications/{state['app']['package']}/edits", "200")
        self._set_track(state, edit, track, version_code, status, notes, release_name)

    def track(self, track):
        state = self.load()
        package = state["app"]["package"]
        edit = self._edit(state)
        for line in (f"POST /applications/{package}/edits", f"GET /applications/{package}/edits/{edit}/tracks/{track}",
                     f"DELETE /applications/{package}/edits/{edit}"):
            self.record(state, line, "200" if not line.startswith("DELETE") else "204")
        self.save(state)
        return state["tracks"].get(track, {"track": track, "releases": []})

    def bundles(self):
        return self.load()["bundles"]
