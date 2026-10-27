"""Stages 7 and 11 for store-floor: did the apps arrive where the release sent them?

    python scripts/demos/mobile/check.py store-floor staging --version 2.4.0 --url <app or build page>

jamf    staging: Store Floor is the new version and scoped to Pilot only, and
        every pilot device reports it in Jamf inventory. Devices that haven't
        reported by half the timeout get the update command.
        production: the scope is All store devices, the pilot still reports
        the version, and how many store devices have it so far.
stores  staging: TestFlight has processed the build, and Google Play's internal
        track holds it as a draft. production: the build is in testing for
        TestFlight's internal group and released on the internal track, both
        with the release notes.

After a rollback the same checks look for the version and build gone back to.
Prints one line per check, "ok   ..." or "FAIL ...", and exits 1 on a failure.
"""

import argparse
import time

import mobile
from mobile import SERVICE
from standin import ServiceError

import run_state  # noqa: E402  (scripts/, through mobile)


class Checks:
    def __init__(self):
        self.failed = False

    def line(self, ok, text):
        self.failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {text}")
        return ok


def expected(state, environment, version):
    deploy = state["deploys"][environment]
    if version == deploy.get("rolled_back_to"):
        return version, str(deploy["rolled_back_number"])
    return version, str(deploy["number"])


def check_jamf(state, environment, version, timeout, checks, clock=time.monotonic, sleep=time.sleep):
    from jamf_apps import jamf

    config = mobile.config()
    settings = config["jamf"]
    bundle = config["app"]["bundle_id"]
    version, build = expected(state, environment, version)
    rolled_back = version == state["deploys"][environment].get("rolled_back_to")
    client = jamf()
    app = client.find_app(settings["app"])
    scope = [settings["pilot_group"]] if environment == "staging" else [settings["all_group"]]
    if rolled_back:
        scope = app["scope"]
    checks.line((app["version"], str(app["build"])) == (version, build),
                f"Jamf: {settings['app']} is {app['version']} ({app['build']}), expected {version} ({build})")
    checks.line(app["scope"] == scope, f"Jamf: scoped to {', '.join(app['scope'])}"
                + ("" if app["scope"] == scope else f", expected {', '.join(scope)}"))

    pilots = client.devices(settings["pilot_group"])
    started, nudged = clock(), False
    while True:
        reports = {d["id"]: client.installed(d["id"], bundle) for d in pilots}
        waiting = [d for d in pilots if not reports[d["id"]] or
                   (reports[d["id"]]["version"], str(reports[d["id"]]["build"])) != (version, build)]
        if not waiting or clock() - started >= timeout:
            break
        if not nudged and clock() - started >= timeout / 2:
            client.send_update([d["id"] for d in waiting])
            print(f"     sent the update command to {', '.join(d['name'] for d in waiting)}")
            nudged = True
        sleep(10)
    for device in pilots:
        report = reports[device["id"]]
        seen = f"{report['version']} ({report['build']})" if report else "nothing"
        checks.line(device not in waiting, f"{device['name']} reports Store Floor {seen}")
    if environment == "production" and not checks.failed:
        everyone = client.devices(settings["all_group"])
        have = sum(1 for d in everyone if (client.installed(d["id"], bundle) or {}).get("version") == version)
        print(f"ok   {have} of {len(everyone)} store devices report {version} so far; "
              "the rest install it when they check in")


def check_stores(state, environment, version, timeout, checks, clock=time.monotonic, sleep=time.sleep):
    from google_play import google_play
    from testflight import testflight

    config = mobile.config()
    group, track = config["testflight"]["group"], config["google_play"]["track"]
    deploy = state["deploys"][environment]
    store, play = testflight(), google_play()

    if version == deploy.get("rolled_back_to") or deploy.get("rolled_back_number"):
        found = store.build(state["version"], deploy["number"])
        checks.line(bool(found and found["expired"]), f"TestFlight: build {deploy['number']} expired")
        live = play.track(track)["releases"]
        codes = live[0]["versionCodes"] if live else []
        checks.line(str(deploy["rolled_back_number"]) in codes,
                    f"Google Play: the {track} track has version code {', '.join(codes) or 'nothing'}")
        return

    version, build = expected(state, environment, version)
    started = clock()
    while True:
        found = store.build(version, build, group)
        if (found and found["processing"] in ("VALID", "INVALID", "FAILED")) or clock() - started >= timeout:
            break
        sleep(30)
    if not checks.line(bool(found) and found["processing"] == "VALID",
                       f"TestFlight: {version} ({build}) processed: {found['processing'] if found else 'not there yet'}"):
        return
    if environment == "staging":
        checks.line(found["internal_state"] != "MISSING_EXPORT_COMPLIANCE",
                    f"TestFlight: export compliance {('answered by Info.plist' if found['internal_state'] != 'MISSING_EXPORT_COMPLIANCE' else 'missing')}")
    else:
        checks.line(group in found["groups"] and found["internal_state"] == "IN_BETA_TESTING",
                    f"TestFlight: {found['internal_state'].replace('_', ' ').lower()} for the internal group "
                    f"{group if group in found['groups'] else '(not added)'}")
        checks.line(bool(found["whats_new"]), f"TestFlight: What to Test: {(found['whats_new'] or 'empty').splitlines()[0]}")

    releases = play.track(track)["releases"]
    release = next((r for r in releases if str(deploy["version_code"]) in r["versionCodes"]), None)
    status = "draft" if environment == "staging" else "completed"
    checks.line(bool(release) and release["status"] == status,
                f"Google Play: version code {deploy['version_code']} on the {track} track: "
                f"{release['status'] if release else 'not there'}")
    if environment == "production" and release:
        notes = (release.get("releaseNotes") or [{}])[0].get("text") or ""
        checks.line(bool(notes), f"Google Play: release notes: {(notes or 'none').splitlines()[0]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", choices=[SERVICE])
    parser.add_argument("environment", choices=["staging", "production"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", default="")
    args = parser.parse_args()

    config = mobile.config()
    state = run_state.load()
    timeout = config["environments"][args.environment]["check"]["timeout_seconds"]
    checks = Checks()
    if args.url:
        print(f"     {args.url}")
    try:
        check = check_jamf if state.get("option") == "jamf" else check_stores
        check(state, args.environment, args.version, timeout, checks)
    except ServiceError as error:
        checks.line(False, error.message)
    except KeyError as error:
        checks.line(False, f"this run has no {args.environment} deploy to check ({error})")
    name = f"{args.environment.capitalize()} check"
    print(f"{name} failed" if checks.failed else f"{name} passed")
    raise SystemExit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
