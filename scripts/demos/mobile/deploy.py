"""Stages 6 and 10 for store-floor: hand the signed apps to Jamf, or to the stores' testers.

    python scripts/demos/mobile/deploy.py store-floor staging
    python scripts/demos/mobile/deploy.py store-floor production
    python scripts/demos/mobile/deploy.py store-floor production --back       back to where this run found it
    python scripts/demos/mobile/deploy.py store-floor production --previous   the release before the live one (/rollback)

The release's option decides where the apps go:

jamf    staging: the ad hoc .ipa to the in-house app Store Floor, scoped to Pilot.
        production, after the approval: the scope widens to All store devices.
stores  staging: the App Store .ipa to TestFlight and the .aab to Google Play's
        internal track, as a draft. production: the build goes to the internal
        testers, in TestFlight's group and on the track, with the release notes
        from the Jira version.

Every deploy downloads the package from JFrog Container Registry, checks its
SHA-256 against the run, and each file inside against SHA256SUMS. iOS refuses
an older build over a newer one, so going back on Jamf rebuilds the previous
version's code with a new, higher build number (store-floor-build on the Mac)
and uploads that. Jamf, TestFlight and Google Play run as stand-ins while
STANDIN in services.yaml says so.
"""

import argparse
import io
import json
import subprocess
import sys
import urllib.parse
import zipfile

import mobile
from mobile import ROOT, SERVICE, fail
from standin import ServiceError

import run_state  # noqa: E402  (scripts/, through mobile)
from common import get_parameter, ssm_client  # noqa: E402
from publish import candidates_root, download, releases_root, request as jcr_request, upload  # noqa: E402


def package_files(state):
    """The package from JCR, checked as a whole and file by file."""
    token = get_parameter(ssm_client(), "jcr/token")
    data = download(token, state["package"]["path"])
    actual = mobile.sha256(data)
    if actual != state["package"]["sha256"]:
        fail(f"{state['package']['path']} has SHA-256 {actual}, but the build recorded {state['package']['sha256']}. "
             "Not deploying a package that changed after it was published.")
    print(f"SHA-256 verified: {actual}")
    archive = zipfile.ZipFile(io.BytesIO(data))
    files = {}
    for line in archive.read("SHA256SUMS").decode().splitlines():
        digest, name = line.split("  ", 1)
        content = archive.read(name)
        if mobile.sha256(content) != digest:
            fail(f"{name} in the package doesn't match its SHA-256 in SHA256SUMS. Not deploying it.")
        files[name] = content
    return files


def pick(files, suffix):
    matches = [name for name in files if name.endswith(suffix)]
    if len(matches) != 1:
        fail(f"The package has {len(matches)} files ending in {suffix}, expected one.")
    return matches[0].split("/")[-1], files[matches[0]]


def note(text):
    """A work note on the change, once it exists (change.py skips it before)."""
    subprocess.run([sys.executable, str(ROOT / "scripts" / "change.py"), "note", text], check=False)


def release_notes(state):
    """The Jira version's description, as Mateusz writes it, or the summaries of its issues."""
    name = f"{SERVICE} {state['version']}"
    try:
        from jira import Jira

        jira = Jira()
        version = jira.version(name, create=False) or {}
        if version.get("description"):
            return version["description"].strip()
        issues = jira.call("GET", "/rest/api/3/search/jql?fields=summary&jql=" +
                           urllib.parse.quote(f'fixVersion = "{name}" ORDER BY key'))
        lines = [f"- {i['fields']['summary']} ({i['key']})" for i in issues.get("issues", [])]
        if lines:
            return f"Store Floor {state['version']}\n" + "\n".join(lines)
    except SystemExit:
        pass
    return f"Store Floor {state['version']} ({state['jira_key']})"


def next_rebuild_number(*builds):
    """A build above every one given: "57" gives "57.1", "57.1" gives "57.2"."""
    from jamf_apps import build_key

    highest = max((b for b in builds if b), key=build_key)
    parts = list(build_key(highest)) + [0]
    return f"{parts[0]}.{parts[1] + 1}"


def source_of(version):
    """Where the code of an earlier release is: its commit, from the release copy
    in JCR, or the demo state of that version."""
    token = get_parameter(ssm_client(), "jcr/token")
    status, body = jcr_request("GET", f"{releases_root(SERVICE)}/{version}/build-info.json", token)
    if status == 200:
        commit = json.loads(body).get("commit", "")
        if commit and not commit.endswith("-dirty") and commit != "uncommitted":
            return commit
    if (ROOT / "demo-states" / SERVICE / version).is_dir():
        return f"demo-state:{version}"
    fail(f"No code for {SERVICE} {version}: neither a release copy in JCR nor demo-states/{SERVICE}/{version}.")


def rebuild(version, number):
    """The previous version's ad hoc .ipa, rebuilt with a higher build number,
    and stored in JCR next to the other candidates."""
    import jenkins

    source = source_of(version)
    print(f"iOS keeps a newer build over an older one: rebuilding {version} from {source} as build {number}")
    parts = jenkins.build(version, number, source, ["ios"], build_orders_url())
    name = f"{SERVICE}-{version}-{number}-adhoc.ipa"
    data = (parts / "ios" / name if (parts / "ios" / name).exists() else parts / name).read_bytes()
    path = f"{candidates_root(SERVICE)}/{version}/{number}/{name}"
    upload(get_parameter(ssm_client(), "jcr/token"), path, data)
    print(f"Stored {path}")
    return name, data


def build_orders_url():
    from build import orders_url

    return orders_url()


# --- Jamf -------------------------------------------------------------------


def jamf_deploy(state, environment, config, back, previous):
    from jamf_apps import jamf

    settings = config["jamf"]
    client = jamf()
    app = client.find_app(settings["app"])
    deploys = state.setdefault("deploys", {})
    if back or previous:
        if back:
            target_version = deploys[environment]["previous_version"]
        else:
            target_version = previous_release(app["version"])
            deploys[environment] = {"previous_number": app["build"], "previous_version": app["version"]}
        # Above what Jamf holds and what this run uploaded. Not the job's own
        # number: a rollback job's count has nothing to do with store builds.
        number = next_rebuild_number(app["build"], deploys[environment].get("number"))
        name, data = rebuild(target_version, number)
        client.upload_ipa(app["id"], name, data)
        deploys[environment].update({"rolled_back_to": target_version, "rolled_back_number": number, "url": app["url"]})
        run_state.save(state)
        print(f"Jamf: {settings['app']} is back on {target_version} ({number}), scope {', '.join(app['scope'])}")
        note(f"Jamf: {settings['app']} back on {target_version}, rebuilt as build {number} (iOS won't install an "
             f"older build over a newer one). Scope: {', '.join(app['scope'])}.")
        return

    if environment == "staging":
        files = package_files(state)
        name, data = pick(files, "-adhoc.ipa")
        info = mobile.ipa_info(data)
        if app["scope"] != [settings["pilot_group"]]:
            client.set_scope(app["id"], [settings["pilot_group"]])
            print(f"Jamf: scope set to {settings['pilot_group']} before the upload (was {', '.join(app['scope'])})")
        client.upload_ipa(app["id"], name, data)
        deploys["staging"] = {"number": info["build"], "previous_number": app["build"],
                              "previous_version": app["version"], "url": app["url"], "deployed_at": run_state.now(),
                              "app_id": app["id"], "scope": [settings["pilot_group"]]}
        state["change_details"] = [f"Jamf in-house app: {settings['app']} {info['version']} ({info['build']}), "
                                   f"ad hoc, scoped to {settings['pilot_group']} until this change is approved"]
        run_state.save(state)
        print(f"Jamf: {settings['app']} {info['version']} ({info['build']}) uploaded, scoped to {settings['pilot_group']}")
        return

    staging = deploys["staging"]
    client.set_scope(app["id"], [settings["all_group"]])
    deploys["production"] = {"number": staging["number"], "previous_number": staging["previous_number"],
                             "previous_version": staging["previous_version"], "url": app["url"],
                             "deployed_at": run_state.now(), "app_id": app["id"], "scope": [settings["all_group"]]}
    run_state.save(state)
    print(f"Jamf: {settings['app']} {app['version']} ({app['build']}) scope widened from "
          f"{', '.join(app['scope'])} to {settings['all_group']}")
    note(f"Jamf: {settings['app']} {app['version']} ({app['build']}), scope widened from {', '.join(app['scope'])} "
         f"to {settings['all_group']} after the approval.")


def previous_release(live):
    """The highest released version below the live one: JCR's release copies,
    or the demo states."""
    from jamf_apps import build_key

    token = get_parameter(ssm_client(), "jcr/token")
    status, body = jcr_request("GET", f"api/storage/{releases_root(SERVICE)}", token)
    versions = set()
    if status == 200:
        versions |= {c["uri"].strip("/") for c in json.loads(body).get("children", []) if c.get("folder")}
    versions |= {p.name for p in (ROOT / "demo-states" / SERVICE).iterdir() if p.is_dir()}
    older = [v for v in versions if build_key(v) < build_key(live)]
    if not older:
        fail(f"No release of {SERVICE} before {live} to roll back to.")
    return max(older, key=build_key)


# --- TestFlight and Google Play ---------------------------------------------


def stores_deploy(state, environment, config, back, previous):
    from google_play import google_play
    from testflight import testflight

    group, track = config["testflight"]["group"], config["google_play"]["track"]
    store, play = testflight(), google_play()
    deploys = state.setdefault("deploys", {})
    if back or previous:
        current = deploys.get(environment) or {}
        if previous and not current.get("number"):
            live = play.track(track)["releases"]
            current = {"number": live[0]["versionCodes"][0] if live else None}
            deploys[environment] = current
        if current.get("testflight_id"):
            store.expire(current["testflight_id"])
            print(f"TestFlight: build {current['number']} expired; testers keep the builds before it")
        target = current.get("previous_number")
        if target:
            play.set_release(track, target, "completed", current.get("previous_notes"), current.get("previous_name"))
            print(f"Google Play: the {track} track is back on version code {target}")
        current.update({"rolled_back_to": current.get("previous_version"), "rolled_back_number": target})
        run_state.save(state)
        note(f"Rolled back: TestFlight build {current.get('number')} expired, Google Play {track} track back on "
             f"version code {target}.")
        return

    notes = release_notes(state)
    if environment == "staging":
        files = package_files(state)
        ipa_name, ipa = pick(files, "-appstore.ipa")
        aab_name, aab = pick(files, ".aab")
        info = mobile.ipa_info(ipa)
        before = (play.track(track)["releases"] or [{}])[0]
        uploaded = store.upload(ipa_name, ipa, info["version"], info["build"])
        print(f"TestFlight: uploaded {ipa_name}, {info['version']} ({info['build']})")
        version_code = play.upload_and_release(track, aab_name, aab, "draft", notes, f"{info['version']} ({info['build']})")
        print(f"Google Play: version code {version_code} uploaded to the {track} track as a draft")
        deploys["staging"] = {
            "number": info["build"], "version_code": version_code, "testflight_id": uploaded["id"],
            "previous_number": (before.get("versionCodes") or [None])[0],
            "previous_version": (before.get("name") or "").split(" ")[0] or None,
            "previous_notes": (before.get("releaseNotes") or [{}])[0].get("text"),
            "previous_name": before.get("name"),
            "url": f"https://appstoreconnect.apple.com/apps/{config['app']['bundle_id']}/testflight/ios",
            "deployed_at": run_state.now(), "release_notes": notes}
        state["change_details"] = [f"TestFlight build: {info['version']} ({info['build']})",
                                   f"Google Play version code: {version_code} ({track} track)"]
        run_state.save(state)
        return

    staging = deploys["staging"]
    store.release_to_group(staging["testflight_id"], group, notes)
    print(f"TestFlight: build {staging['number']} available to the internal group {group}")
    play.set_release(track, staging["version_code"], "completed", notes, f"{state['version']} ({staging['number']})")
    print(f"Google Play: version code {staging['version_code']} released on the {track} track")
    deploys["production"] = {**{k: staging[k] for k in ("number", "version_code", "testflight_id", "previous_number",
                                                           "previous_version", "previous_notes", "previous_name", "url")},
                             "deployed_at": run_state.now(), "release_notes": notes}
    run_state.save(state)
    note(f"Testers have {SERVICE} {state['version']}: TestFlight build {staging['number']} for the internal group "
         f"{group}, Google Play version code {staging['version_code']} on the {track} track. "
         f"Release notes from the Jira version.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", choices=[SERVICE])
    parser.add_argument("environment", choices=["staging", "production"])
    back = parser.add_mutually_exclusive_group()
    back.add_argument("--back", action="store_true", help="back to where this run found it")
    back.add_argument("--previous", action="store_true", help="the release before the live one")
    args = parser.parse_args()

    config = mobile.config()
    state = run_state.load()
    option = state.get("option") or ("jamf" if args.previous else "")
    if option not in config["slack"]["options"]:
        fail(f"Say where store-floor goes: /release {SERVICE} {state.get('jira_key', 'ORD-80')} "
             f"{' or '.join(config['slack']['options'])}.")
    deploy = jamf_deploy if option == "jamf" else stores_deploy
    try:
        deploy(state, args.environment, config, args.back, args.previous)
    except ServiceError as error:
        fail(f"{option}: {error.message}")


if __name__ == "__main__":
    main()
