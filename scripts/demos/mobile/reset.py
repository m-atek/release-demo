"""Put demo 7 back to its starting state, for a retake or a call. Safe to run twice.

    python scripts/reset.py mobile              on the tools host (from the Mac it hands itself over)
    python scripts/reset.py mobile --dry-run    every action it would take, and nothing else
    python scripts/demos/mobile/reset.py --standins   only the stand-ins, from anywhere, no rebuild

The starting state: every store device on Store Floor 2.3.0, rebuilt with a
build number above the last upload (iOS won't install an older build over a
newer one), and the app scoped to Pilot again; 2.3.0 the testers' latest;
2.4.0 staged on main; ORD-80 and ORD-81 open and the Jira version
"store-floor 2.4.0" unreleased; no open store-floor change.

Like scripts/reset.py for the main demo, it keeps the past and archives
instead of deleting, with that script's own steps: earlier takes' tags move to
takes/<tag>-<UTC time> with their GitHub releases, their release copies in
JFrog to <releases root>/takes/, and open changes are cancelled, not removed.

The rebuild runs store-floor-build on the Mac agent, so the full reset runs on
the tools host, which reaches Jenkins and JFrog. --standins resets the
stand-ins alone, with no rebuild, for a call without the Mac.
"""

import argparse
import sys
import time

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

    standin_export = mobile.local("build").standin_export

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
    next_rebuild_number = mobile.local("deploy").next_rebuild_number

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
    next_rebuild_number = mobile.local("deploy").next_rebuild_number

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
    deploy = mobile.local("deploy")
    next_rebuild_number, rebuild = deploy.next_rebuild_number, deploy.rebuild

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


def platform_reset():
    """scripts/reset.py, the main demo's reset, with its steps pointed at store-floor."""
    import importlib.util

    # It imports scripts/check.py, build.py and deploy.py, which this folder's
    # scripts of the same names would hide: scripts/ goes first while it loads.
    clashing = ("build", "check", "deploy")
    ours = {name: sys.modules.pop(name) for name in clashing if name in sys.modules}
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location("platform_reset", ROOT / "scripts" / "reset.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts)
        for name in clashing:
            sys.modules.pop(name, None)
        sys.modules.update(ours)
    module.SERVICE, module.BASELINE, module.DEMO_ISSUES = SERVICE, START, ISSUES
    return module


def stage_release(reset, host):
    current = host.main_version(mobile.config()["path"])
    if current == RELEASE:
        print(f"  main has {SERVICE} {RELEASE}")
        return
    reset.act(f"stage {SERVICE} {RELEASE} on main (main has {current})", lambda: host.stage(SERVICE, RELEASE))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--standins", action="store_true", help="only the stand-ins' state, with no rebuild")
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--follow", action="store_true", help="from the Mac: show the run on the tools host")
    args = parser.parse_args()
    started = time.time()
    store = mobile.state_store() if mobile.standins() else None

    if args.standins:
        if not store:
            mobile.fail("No stand-ins: STANDIN is 0 for store-floor.")
        if args.dry_run:
            say("Would put the stand-ins back on 2.3.0, scope Pilot")
            return
        reset_standins(store)
        say(f"Stand-ins reset in {time.time() - started:.0f}s")
        return

    import host

    if not args.here and not host.on_host():
        shown = ["--dry-run"] if args.dry_run else []
        sys.exit(host.hand_over("reset-mobile", "demos/mobile/reset.py", shown, follow_only=args.follow,
                                title=" ".join(["reset.py mobile", *shown])))
    sys.stdout.reconfigure(line_buffering=True)
    platform = platform_reset()
    reset = platform.Reset(args.dry_run)
    print(f"Reset demo 7 to {SERVICE} {START} on the devices and {RELEASE} on main, keeping the past"
          f"{' (dry run: nothing changes)' if args.dry_run else ''}.")
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: platform.stop_runs(
            reset, platform.release_jobs()))
    except platform.HistoryRunning as error:
        print(f"  {error}")
        sys.exit(1)
    reset.step("2. Open store-floor changes in ServiceNow", lambda: platform.cancel_changes(reset))
    reset.step(f"3. The take's tags and GitHub releases, after {START}", lambda: platform.github_takes(reset))
    reset.step(f"4. The take's release copies in JFrog, after {START}", lambda: platform.jfrog_takes(reset))
    reset.step(f"5. Jira versions after {START}, and {' and '.join(ISSUES)}", lambda: platform.jira_versions(reset))
    reset.step(f"6. {SERVICE} {RELEASE} on main", lambda: stage_release(reset, host))
    if store and not store.read("testflight.json"):
        reset.act("give the stand-ins their starting state", lambda: initial_standins(store))
    reset.step(f"7. Store devices on {START}, rebuilt with a higher build, scope Pilot",
               lambda: reset.act(f"rebuild {START} on the Mac and give it to every store device", lambda: reset_jamf(store)))
    if store and {"apple", "google-play"} <= mobile.standins():
        reset.step(f"8. {START} the testers' latest", lambda: reset.act(
            "expire 2.4.0 in TestFlight's stand-in and put 2.3.0 back on the internal track",
            lambda: reset_store_tracks(store)))
    took = time.time() - started
    if reset.failed:
        print(f"Finished in {took:.0f}s, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        sys.exit(1)
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s).")


if __name__ == "__main__":
    main()
