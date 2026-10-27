"""Jamf Pro's in-house app for store devices: Store Floor, the real API or its stand-in.

Both answer the same calls, from the Classic API the spec names:

    find_app(name)                     GET  /JSSResource/mobiledeviceapplications/name/{name}
    upload_ipa(app_id, name, data)     POST /JSSResource/fileuploads/mobiledeviceapplicationsipa/id/{id}
    set_scope(app_id, groups)          PUT  /JSSResource/mobiledeviceapplications/id/{id}
    devices(group)                     GET  /JSSResource/mobiledevicegroups/name/{group}
    installed(device_id, bundle_id)    GET  /JSSResource/mobiledevices/id/{id}/subset/Applications
    send_update(device_ids)            POST /JSSResource/mobiledevicecommands/command/BlankPush/id/{ids}
                                       and UpdateInventory, so the devices check in and report

With In-House App Maintenance's automatic updates on, a device in scope
installs a new upload by itself; send_update wakes the ones that haven't.
One app record whose scope is Pilot until the change is approved: a real fleet
would keep a second record for everyone else, because Jamf removes a managed
app from devices that leave its scope. The demo's devices never leave it.

Demo 6's Jamf stand-in for Macs is scripts/demos/standins/jamf.py.
"""

import json
import time
import urllib.parse
import uuid
from xml.sax.saxutils import escape

import mobile
from standin import ServiceError, Standin, http, json_or_none


def build_key(build):
    """CFBundleVersion as numbers: "57.1" comes after "57" and before "58"."""
    return tuple(int(part) for part in str(build).split(".") if part.isdigit())


def jamf(settings=None, store=None):
    if "jamf" in mobile.standins(settings):
        return JamfStandin(store or mobile.state_store())
    ssm = mobile.ssm_client()
    return JamfPro(*(mobile.get_parameter(ssm, f"{mobile.SERVICE}/{name}")
                     for name in ("jamf-url", "jamf-client-id", "jamf-client-secret")))


class JamfPro:
    """Jamf Pro itself, with an API client that may only manage mobile device apps."""

    def __init__(self, url, client_id, client_secret):
        self.url = url.rstrip("/")
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id,
                                       "client_secret": client_secret}).encode()
        _, raw = http("POST", f"{self.url}/api/oauth/token", {"Content-Type": "application/x-www-form-urlencoded"}, form)
        self.token = json.loads(raw)["access_token"]

    def call(self, method, path, body=None, content_type=None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        _, raw = http(method, self.url + path, headers, body)
        return json_or_none(raw) if raw[:1] in (b"{", b"[") else raw

    def app_url(self, app_id):
        return f"{self.url}/mobileDeviceApps.html?id={app_id}&o=r"

    def find_app(self, name):
        record = self.call("GET", f"/JSSResource/mobiledeviceapplications/name/{urllib.parse.quote(name)}")
        app = record["mobile_device_application"]
        general, scope = app["general"], app["scope"]
        groups = [g["name"] for g in scope.get("mobile_device_groups", [])]
        return {"id": general["id"], "name": general["name"], "bundle_id": general["bundle_id"],
                "version": general.get("version"), "build": general.get("version"),
                "scope": ["All mobile devices"] if scope.get("all_mobile_devices") else groups,
                "url": self.app_url(general["id"])}

    def upload_ipa(self, app_id, filename, data):
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        self.call("POST", f"/JSSResource/fileuploads/mobiledeviceapplicationsipa/id/{app_id}", body,
                  f"multipart/form-data; boundary={boundary}")

    def set_scope(self, app_id, groups):
        targets = "".join(f"<mobile_device_group><name>{escape(g)}</name></mobile_device_group>" for g in groups)
        xml = (f"<mobile_device_application><scope><all_mobile_devices>false</all_mobile_devices>"
               f"<mobile_device_groups>{targets}</mobile_device_groups></scope></mobile_device_application>")
        self.call("PUT", f"/JSSResource/mobiledeviceapplications/id/{app_id}", xml.encode(), "application/xml")

    def devices(self, group):
        record = self.call("GET", f"/JSSResource/mobiledevicegroups/name/{urllib.parse.quote(group)}")
        return [{"id": d["id"], "name": d["name"]} for d in record["mobile_device_group"]["mobile_devices"]]

    def installed(self, device_id, bundle_id):
        record = self.call("GET", f"/JSSResource/mobiledevices/id/{device_id}/subset/Applications")
        for app in record["mobile_device"]["applications"]:
            if app.get("identifier") == bundle_id:
                return {"version": app.get("application_short_version") or app.get("application_version"),
                        "build": app.get("application_version"), "reported_at": None}
        return None

    def send_update(self, device_ids):
        ids = ",".join(str(i) for i in device_ids)
        for command in ("BlankPush", "UpdateInventory"):
            self.call("POST", f"/JSSResource/mobiledevicecommands/command/{command}/id/{ids}")


STORES = ["Utrecht Centraal", "Amsterdam Zuid", "Rotterdam Blaak", "Eindhoven Strijp", "Den Haag Spui",
          "Groningen Vismarkt"]


def initial_state(app_name, bundle_id, version, build, clock=time.time):
    """Twelve store devices, an iPad and an iPhone per store. Utrecht Centraal's
    two are the Pilot group. Every device runs the same Store Floor build."""
    now = clock()
    devices = {}
    for number, (store, model) in enumerate(((s, m) for s in STORES for m in ("iPad", "iPhone")), start=1):
        devices[str(number)] = {"id": number, "name": f"{store} {model}", "store": store, "model": model,
                                "apps": {bundle_id: {"version": version, "build": str(build)}},
                                "last_inventory": now, "last_result": "Installed"}
    return {
        "apps": {"7": {"id": 7, "name": app_name, "bundle_id": bundle_id, "version": version, "build": str(build),
                       "file": None, "uploaded_at": now, "scope": ["Pilot"], "automatic_updates": True}},
        "groups": {"Pilot": [1, 2], "All store devices": list(range(1, 13))},
        "devices": devices,
        "requests": [],
    }


class JamfStandin(Standin):
    key, name, switch = "jamf.json", "Jamf Pro", "jamf"

    def app_url(self, app_id):
        return f"http://127.0.0.1:8077/jamf#app-{app_id}"

    def _app(self, state, app_id):
        app = state["apps"].get(str(app_id))
        if not app:
            raise ServiceError(404, "The server has not found anything matching the request URI")
        return app

    def _in_scope(self, state, app):
        ids = set()
        for group in app["scope"]:
            ids.update(state["groups"].get(group, []))
        return sorted(ids)

    def _install(self, state, app, device_ids):
        """Automatic updates: each device in scope installs the app's current
        build, unless it already has a newer one, which iOS refuses to replace."""
        now = self.clock()
        for device_id in device_ids:
            device = state["devices"][str(device_id)]
            current = device["apps"].get(app["bundle_id"])
            if current and build_key(current["build"]) > build_key(app["build"]):
                device["last_result"] = (f"Failed: build {current['build']} is installed, "
                                         f"which is newer than {app['build']}")
                continue
            device["apps"][app["bundle_id"]] = {"version": app["version"], "build": app["build"]}
            device["last_inventory"], device["last_result"] = now, "Installed"

    def find_app(self, name):
        state = self.load()
        self.record(state, f"GET /JSSResource/mobiledeviceapplications/name/{urllib.parse.quote(name)}")
        self.save(state)
        for app in state["apps"].values():
            if app["name"] == name:
                return {**{k: app[k] for k in ("id", "name", "bundle_id", "version", "build")},
                        "scope": list(app["scope"]), "url": self.app_url(app["id"])}
        raise ServiceError(404, "The server has not found anything matching the request URI")

    def upload_ipa(self, app_id, filename, data):
        state = self.load()
        request = f"POST /JSSResource/fileuploads/mobiledeviceapplicationsipa/id/{app_id}"
        app = self._app(state, app_id)
        try:
            info = mobile.ipa_info(data)
        except (ValueError, KeyError) as error:
            self.record(state, request, "409")
            self.save(state)
            raise ServiceError(409, f"Unable to read the IPA: {error}") from None
        problem = None
        if info["bundle_id"] != app["bundle_id"]:
            problem = f"The bundle identifier of the IPA ({info['bundle_id']}) doesn't match the app ({app['bundle_id']})"
        elif not info["signed"]:
            problem = "The IPA is not signed"
        elif info["method"] not in ("ad-hoc", "enterprise"):
            problem = f"An in-house app needs an ad hoc or enterprise build, not {info['method']}"
        if problem:
            self.record(state, request, "409")
            self.save(state)
            raise ServiceError(409, problem)
        app.update({"version": info["version"], "build": info["build"], "uploaded_at": self.clock(),
                    "file": {"name": filename, "sha256": mobile.sha256(data), "size_bytes": len(data)}})
        self.record(state, request, "201")
        if app.get("automatic_updates"):
            self._install(state, app, self._in_scope(state, app))
        self.save(state)

    def set_scope(self, app_id, groups):
        state = self.load()
        app = self._app(state, app_id)
        unknown = [g for g in groups if g not in state["groups"]]
        if unknown:
            self.record(state, f"PUT /JSSResource/mobiledeviceapplications/id/{app_id}", "409")
            self.save(state)
            raise ServiceError(409, f"Problem with mobile device group in scope: {unknown[0]}")
        app["scope"] = list(groups)
        self.record(state, f"PUT /JSSResource/mobiledeviceapplications/id/{app_id}", "201")
        if app.get("automatic_updates"):
            self._install(state, app, self._in_scope(state, app))
        self.save(state)

    def devices(self, group):
        state = self.load()
        self.record(state, f"GET /JSSResource/mobiledevicegroups/name/{urllib.parse.quote(group)}")
        self.save(state)
        if group not in state["groups"]:
            raise ServiceError(404, "The server has not found anything matching the request URI")
        return [{"id": i, "name": state["devices"][str(i)]["name"]} for i in state["groups"][group]]

    def installed(self, device_id, bundle_id):
        state = self.load()
        self.record(state, f"GET /JSSResource/mobiledevices/id/{device_id}/subset/Applications")
        self.save(state)
        device = state["devices"][str(device_id)]
        app = device["apps"].get(bundle_id)
        return {**app, "reported_at": device["last_inventory"]} if app else None

    def send_update(self, device_ids):
        state = self.load()
        ids = ",".join(str(i) for i in device_ids)
        for command in ("BlankPush", "UpdateInventory"):
            self.record(state, f"POST /JSSResource/mobiledevicecommands/command/{command}/id/{ids}", "201")
        for app in state["apps"].values():
            in_scope = set(self._in_scope(state, app))
            self._install(state, app, [i for i in device_ids if i in in_scope])
        self.save(state)
