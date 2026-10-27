"""Put demo 7 back to its starting state, for a retake or a call. Safe to run twice.

    python scripts/demos/mobile/reset.py              on the tools host, as scripts/reset.py mobile runs it
    python scripts/demos/mobile/reset.py --standins   only the stand-ins' state, from anywhere (no rebuild)

The starting state: every store device on Store Floor 2.3.0, rebuilt with a
build number above the last upload (iOS won't install an older build over a
newer one), and the app scoped to Pilot again; 2.3.0 the latest build for the
testers; 2.4.0 staged on main; ORD-80 and ORD-81 open, the Jira version
"store-floor 2.4.0" unreleased, no tag store-floor-v2.4.0, no release copy of
2.4.0 in JCR, and no open store-floor change in ServiceNow.

The rebuild runs store-floor-build on the Mac agent, so the full reset runs
on the tools host, which reaches Jenkins and JCR. With Jamf as a stand-in and
no Mac, --standins puts the stand-in's devices back on 2.3.0 directly.
"""

import argparse
import base64
import subprocess
import sys
import time
import urllib.error
import urllib.request

import mobile
from mobile import ROOT, SERVICE

import google_play  # noqa: E402
import jamf_apps  # noqa: E402
import testflight  # noqa: E402

START, RELEASE = "2.3.0", "2.4.0"
ISSUES = ["ORD-80", "ORD-81"]


def say(text):
    print(text, flush=True)


def initial_standins(store, build="1"):
    """The stand-ins as they start: every device, TestFlight and the internal track on 2.3.0."""
    config = mobile.config()
    app = config["app"]
    store.write("jamf.json", jamf_apps.initial_state(config["jamf"]["app"], app["bundle_id"], START, build))
    flight = testflight.initial_state(app["name"], app["bundle_id"], config["testflight"]["group"])
    flight["builds"].append({"id": "b0001", "version": START, "build": build, "uploaded_at": time.time(),
                             "processing": "VALID", "expired": False, "internal_state": "IN_BETA_TESTING",
                             "groups": [config["testflight"]["group"]],
                             "whats_new": f"Store Floor {START}: today's orders on the store floor."})
    store.write("testflight.json", flight)
    play = google_play.initial_state(app["name"], app["package_name"], config["google_play"]["track"])
    play["bundles"].append({"version_code": int(build.split(".")[0]), "version_name": START, "target_sdk": 36,
                            "sha256": None, "size_bytes": None, "uploaded_at": time.time(), "file": None})
    play["tracks"][config["google_play"]["track"]]["releases"] = [{
        "name": f"{START} ({build})", "versionCodes": [build.split(".")[0]], "status": "completed",
        "releaseNotes": [{"language": "en-US", "text": f"Store Floor {START}: today's orders on the store floor."}],
        "updated_at": time.time()}]
    store.write("google-play.json", play)


def placeholder_ipa(version, build):
    """For the stand-ins only, when no Mac can rebuild: an .ipa holding just
    2.3.0's Info.plist, sealed like the Apple stand-in's exports, so Jamf's
    stand-in takes it through the same upload call."""
    import plistlib
    import tempfile
    from pathlib import Path

    from build import standin_export

    config = mobile.config()
    with tempfile.TemporaryDirectory() as temp:
        app = Path(temp) / "Runner.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": config["app"]["bundle_id"], "CFBundleShortVersionString": version,
            "CFBundleVersion": build, "CFBundleDisplayName": config["app"]["name"],
            "ITSAppUsesNonExemptEncryption": False}))
        target = Path(temp) / "placeholder.ipa"
        standin_export(app, "ad-hoc", target)
        return target.read_bytes()


def reset_shared_jamf(client):
    """Demo 6's shared Jamf stand-in: Store Floor exists, every store device gets
    2.3.0 with a build above the last, and the scope goes back to Pilot."""
    from deploy import next_rebuild_number

    config = mobile.config()
    settings, app_config = config["jamf"], config["app"]
    client.ensure_app(settings["app"], app_config["bundle_id"], settings["pilot_group"])
    app = client.find_app(settings["app"])
    on_start = app["version"] == START and app["scope"] == [settings["pilot_group"]] and all(
        (client.installed(d["id"], app_config["bundle_id"]) or {}).get("version") == START
        for d in client.devices(settings["all_group"]))
    if on_start:
        say(f"Jamf: already on {START}, scope {settings['pilot_group']}")
        return
    builds = [(client.installed(d["id"], app_config["bundle_id"]) or {}).get("build")
              for d in client.devices(settings["all_group"])]
    number = next_rebuild_number("1", app["build"], *builds)
    client.set_scope(app["id"], [settings["all_group"]])
    client.upload_ipa(app["id"], f"{SERVICE}-{START}-{number}-adhoc.ipa", placeholder_ipa(START, number))
    client.put_back(app_config["bundle_id"], settings["app"], START, number, settings["all_group"])
    client.set_scope(app["id"], [settings["pilot_group"]])
    say(f"Jamf: every store device on {START} ({number}), scope {settings['pilot_group']}")


def reset_standins(store):
    """Back to the start without a rebuild: the stand-in's devices on 2.3.0 with
    a higher build number, scope Pilot, and 2.3.0 the testers' latest."""
    client = jamf_apps.jamf()
    if isinstance(client, jamf_apps.SharedJamf):
        if not (store.read("testflight.json") and store.read("google-play.json")):
            initial_standins(store)
        reset_shared_jamf(client)
        reset_store_tracks(store)
        return
    jamf_state = store.read("jamf.json")
    if not (jamf_state and store.read("testflight.json") and store.read("google-play.json")):
        initial_standins(store)
        say("Stand-ins: created, everything on 2.3.0")
        return
    from deploy import next_rebuild_number

    config = mobile.config()
    bundle = config["app"]["bundle_id"]
    app = next(iter(jamf_state["apps"].values()))
    on_start = app["version"] == START and app["scope"] == [config["jamf"]["pilot_group"]] and all(
        (d["apps"].get(bundle) or {}).get("version") == START for d in jamf_state["devices"].values())
    if not on_start:
        build = next_rebuild_number(app["build"], *[(d["apps"].get(bundle) or {}).get("build")
                                                    for d in jamf_state["devices"].values()])
        app.update({"version": START, "build": build, "uploaded_at": time.time(), "scope": [config["jamf"]["pilot_group"]],
                    "file": {"name": f"{SERVICE}-{START}-{build}-adhoc.ipa", "sha256": None, "size_bytes": None}})
        for device in jamf_state["devices"].values():
            device["apps"][bundle] = {"version": START, "build": build}
            device["last_inventory"], device["last_result"] = time.time(), "Installed"
        store.write("jamf.json", jamf_state)
        say(f"Jamf stand-in: every device on {START} ({build}), scope {config['jamf']['pilot_group']}")
    else:
        say(f"Jamf stand-in: already on {START}, scope {config['jamf']['pilot_group']}")
    reset_store_tracks(store)


def reset_store_tracks(store):
    config = mobile.config()
    flight = store.read("testflight.json")
    for build in flight["builds"]:
        if build["version"] == RELEASE and not build["expired"]:
            build["expired"], build["internal_state"] = True, "EXPIRED"
    store.write("testflight.json", flight)
    play = store.read("google-play.json")
    track = config["google_play"]["track"]
    start = next((b for b in play["bundles"] if b["version_name"] == START), None)
    current = play["tracks"][track]["releases"]
    if start and (not current or current[0]["versionCodes"] != [str(start["version_code"])]):
        play["tracks"][track]["releases"] = [{
            "name": f"{START} ({start['version_code']})", "versionCodes": [str(start["version_code"])],
            "status": "completed", "updated_at": time.time(),
            "releaseNotes": [{"language": "en-US", "text": f"Store Floor {START}: today's orders on the store floor."}]}]
    store.write("google-play.json", play)
    say(f"TestFlight and Google Play stand-ins: {START} is the testers' latest")


def reset_jamf(store):
    """The real thing: rebuild 2.3.0 above the last build, upload it to every
    device, then narrow the scope back to Pilot."""
    from deploy import next_rebuild_number, rebuild

    config = mobile.config()
    settings = config["jamf"]
    client = jamf_apps.jamf()
    if isinstance(client, jamf_apps.SharedJamf):
        client.ensure_app(settings["app"], config["app"]["bundle_id"], settings["pilot_group"])
    app = client.find_app(settings["app"])
    if app["version"] == START and app["scope"] == [settings["pilot_group"]]:
        say(f"Jamf: already on {START} ({app['build']}), scope {settings['pilot_group']}")
        return
    number = next_rebuild_number(app["build"])
    name, data = rebuild(START, number)
    client.set_scope(app["id"], [settings["all_group"]])
    client.upload_ipa(app["id"], name, data)
    client.set_scope(app["id"], [settings["pilot_group"]])
    say(f"Jamf: {settings['app']} {START} ({number}) on every device, scope {settings['pilot_group']}")


def run(*args):
    result = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
    say("  " + (result.stdout.strip() or result.stderr.strip()).replace("\n", "\n  "))
    return result.returncode == 0


def cancel_open_changes():
    from change import CANCELED, connect

    sn, _ = connect()
    query = f"cmdb_ci.name={SERVICE}^stateNOT IN3,4,7^active=true"
    changes = sn.call("GET", "/api/now/table/change_request", sysparm_query=query, sysparm_fields="sys_id,number,type")
    for change in changes:
        sn.call("PATCH", f"/api/sn_chg_rest/change/{change['type']}/{change['sys_id']}",
                {"state": CANCELED, "close_notes": "Cancelled by reset.py mobile before a retake."})
        say(f"ServiceNow: {change['number']} cancelled")
    if not changes:
        say("ServiceNow: no open store-floor change")


def remove_release_copy():
    """JCR's copy of 2.4.0, so the next take's Record stage can store its own.
    The release-pipeline token can't delete, so this uses the admin password, on the host."""
    from publish import JCR, releases_root

    password = mobile.get_parameter(mobile.ssm_client(), "jcr/admin-password")
    auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    path = f"{releases_root(SERVICE)}/{RELEASE}"
    request = urllib.request.Request(f"{JCR}/artifactory/{path}", method="DELETE", headers={"Authorization": auth})
    try:
        urllib.request.urlopen(request, timeout=30)
        say(f"JCR: {path} removed")
    except urllib.error.HTTPError as error:
        say(f"JCR: no {path}" if error.code == 404 else f"JCR: {path} not removed ({error.code})")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--standins", action="store_true", help="only the stand-ins' state, with no rebuild")
    parser.add_argument("--no-stage", action="store_true", help="don't stage 2.4.0 on main")
    args = parser.parse_args()
    started = time.time()
    store = mobile.state_store() if mobile.standins() else None

    if args.standins:
        if not store:
            mobile.fail("No stand-ins: STANDIN is 0 for store-floor.")
        reset_standins(store)
        say(f"Stand-ins reset in {time.time() - started:.0f}s")
        return

    if store and not store.read("testflight.json"):
        initial_standins(store)
        say("Stand-ins: created, everything on 2.3.0")
    reset_jamf(store)
    if store and {"apple", "google-play"} <= mobile.standins():
        reset_store_tracks(store)
    else:
        say("TestFlight and Google Play: nothing to reset; the next take uploads a higher build")
    ok = True
    if not args.no_stage:
        ok &= run("scripts/stage.py", SERVICE, RELEASE)
    for issue in ISSUES:
        ok &= run("scripts/jira.py", "reopen", issue, f"{SERVICE} {RELEASE}")
    ok &= run("scripts/tag.py", "--delete", f"{SERVICE}-v{RELEASE}")
    remove_release_copy()
    cancel_open_changes()
    say(f"Demo 7 reset in {time.time() - started:.0f}s" + ("" if ok else ", with the problems above"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
