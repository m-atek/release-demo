"""Stage 1, Build, for store-floor: both platforms, signed, in one package.

    python scripts/demos/mobile/build.py store-floor --jira-key ORD-80

In a release this starts the job store-floor-build, which builds iOS on the
Mac agent and Android in the Flutter container on the tools host, at the same
time, and signs both: the iOS archive exported twice, ad hoc for Jamf and App
Store for TestFlight, and the Android App Bundle signed with the upload key.
Then it checks what came back and writes the package the contract asks for:
dist/store-floor/store-floor-<version>.zip with both .ipa files, the .aab, a
SHA256SUMS file and pubspec.lock for the SBOM, plus build-info.json.

The store build number is the release's Jenkins build number, so every upload
is higher than the last. CI (the job ci) builds Android only, unsigned, so a
push never waits for the Mac or touches a key.

The job store-floor-build runs the parts:

    build.py store-floor --platform ios --build-number 57        on the Mac agent
    build.py store-floor --platform android --build-number 57    on the tools host, unsigned
    build.py store-floor --verify-android dist/.../x.aab         after the job's signing step

and by hand, to assemble parts already in dist/store-floor/parts:

    build.py store-floor --jira-key ORD-80 --from-parts
"""

import argparse
import base64
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import mobile
from mobile import ROOT, SERVICE, fail

from common import REGION, git, version_of, write_json  # noqa: E402

ZIP_TIME = (1980, 1, 1, 0, 0, 0)
DIST = ROOT / "dist" / SERVICE
PARTS = DIST / "parts"


def current_commit():
    """Jenkins sets GIT_COMMIT. By hand, HEAD, marked when the tree has changes,
    as scripts/build.py does."""
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"]
    head = git("rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        return "uncommitted"
    dirty = git("status", "--porcelain", "--", mobile.config()["path"], check=False).stdout.strip()
    return head.stdout.strip() + ("-dirty" if dirty else "")


def names(version, number):
    stem = f"{SERVICE}-{version}-{number}"
    return {"adhoc": f"{stem}-adhoc.ipa", "appstore": f"{stem}-appstore.ipa", "aab": f"{stem}.aab"}


def dart_defines(version, number, orders_url):
    defines = [f"--dart-define=APP_VERSION={version}", f"--dart-define=APP_BUILD={number}"]
    return defines + ([f"--dart-define=ORDERS_URL={orders_url}"] if orders_url else [])


def orders_url():
    """orders-service's production URL, for the app's order list. Not secret,
    but kept out of the public repository: Terraform writes it to SSM."""
    if os.environ.get("ORDERS_URL"):
        return os.environ["ORDERS_URL"]
    try:
        return mobile.get_parameter(mobile.ssm_client(), f"{SERVICE}/orders-url")
    except SystemExit:
        print("No orders-service URL in SSM: the app will say it has no orders-service address.")
        return ""


# --- Android: build unsigned, sign in the job's own step, verify ------------


def build_android(version, number, url):
    source = mobile.config()["dir"]
    out = PARTS / "android"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    command = " ".join(["flutter pub get &&", "flutter build appbundle --release", f"--build-name {version}",
                        f"--build-number {number}", *dart_defines(version, number, url)])
    mobile_flutter().in_container(command, cwd=source)
    target = out / names(version, number)["aab"]
    shutil.copyfile(source / "build/app/outputs/bundle/release/app-release.aab", target)
    print(f"Built {shown(target)} (unsigned; the job signs it with the upload key)")
    return target


def verify_android(path, version=None, number=None):
    info = mobile.aab_info(Path(path).read_bytes())
    problems = []
    if not info["signed"]:
        problems.append("not signed")
    if number and info["version_code"] != int(number):
        problems.append(f"version code {info['version_code']}, expected {number}")
    if version and info["version_name"] != version:
        problems.append(f"version name {info['version_name']}, expected {version}")
    if (info["target_sdk"] or 0) < 36:
        problems.append(f"targets API level {info['target_sdk']}, Google Play requires 36")
    for problem in problems:
        print(f"FAIL {Path(path).name}: {problem}")
    if problems:
        raise SystemExit(1)
    print(f"ok   {Path(path).name}: {info['package']} {info['version_name']} ({info['version_code']}), "
          f"target API level {info['target_sdk']}")
    print(f"ok   signed with the upload certificate SHA-256 {info['upload_certificate_sha256']}")
    write_json(Path(path).parent / "android-signing.json", {
        "package": info["package"], "version_code": info["version_code"], "target_sdk": info["target_sdk"],
        "upload_certificate_sha256": info["upload_certificate_sha256"], "signed_by": "upload key (Play App Signing)"})


# --- iOS: build on the Mac, export ad hoc and App Store ---------------------


def build_ios(version, number, url, runner_app=None):
    config = mobile.config()
    source = config["dir"]
    out = PARTS / "ios"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    files = names(version, number)
    if "apple" in mobile.standins():
        if not runner_app:
            mobile_flutter().native(["pub", "get"], cwd=source)
            mobile_flutter().native(["build", "ios", "--release", "--no-codesign", "--build-name", version,
                                     "--build-number", str(number), *dart_defines(version, number, url)], cwd=source)
            runner_app = source / "build/ios/iphoneos/Runner.app"
        summary = {}
        for kind, method in (("adhoc", "ad-hoc"), ("appstore", "app-store-connect")):
            standin_export(Path(runner_app), method, out / files[kind])
            summary[kind] = {"method": method, "identity": "Apple Developer Program stand-in", "file": files[kind]}
        print("Apple Developer Program stand-in: no Apple certificate exists yet, so the stand-in seals both "
              "exports itself. STANDIN without apple signs them with fastlane match.")
    else:
        summary = export_with_match(config, version, number, url, out, files)
    for kind in ("adhoc", "appstore"):
        info = mobile.ipa_info((out / files[kind]).read_bytes())
        print(f"ok   {files[kind]}: {info['bundle_id']} {info['version']} ({info['build']}), {info['method']}")
    write_json(out / "ios-signing.json", summary)


def standin_export(app, method, target):
    """The stand-in's export: the app as built, with a seal like codesign's
    CodeResources (each file's SHA-256) and the export method it was made for."""
    with tempfile.TemporaryDirectory() as temp:
        payload = Path(temp) / "Payload" / app.name
        shutil.copytree(app, payload, symlinks=True)
        (payload / "StandinExport.plist").write_bytes(plistlib.dumps(
            {"method": method, "signed_by": "Apple Developer Program stand-in"}))
        seal = {}
        for path in sorted(p for p in payload.rglob("*") if p.is_file()):
            seal[path.relative_to(payload).as_posix()] = {
                "hash2": base64.b64encode(bytes.fromhex(mobile.file_sha256(path))).decode()}
        (payload / "_CodeSignature").mkdir(exist_ok=True)
        (payload / "_CodeSignature" / "CodeResources").write_bytes(plistlib.dumps({"files2": seal}))
        write_zip(Path(temp), target, prefix="Payload")


def export_with_match(config, version, number, url, out, files):
    """The real path: certificates and profiles from fastlane match (S3, read
    only, password from Jenkins' credentials), in a keychain made for this
    build and deleted after it, also when it fails."""
    source = config["dir"]
    bundle = config["app"]["bundle_id"]
    team = os.environ.get("APPLE_TEAM_ID") or fail("APPLE_TEAM_ID isn't set: the job binds it from Jenkins' credentials.")
    if not os.environ.get("MATCH_PASSWORD"):
        fail("MATCH_PASSWORD isn't set: the job binds it from Jenkins' credentials.")
    bucket = os.environ.get("MATCH_BUCKET") or mobile.get_parameter(mobile.ssm_client(), f"{SERVICE}/bucket")
    keychain = Path(tempfile.mkdtemp()) / "store-floor-build.keychain-db"
    keychain_password = secrets.token_urlsafe(24)
    run = lambda *args, **kw: subprocess.run(list(args), check=True, **kw)
    searched = subprocess.run(["security", "list-keychains", "-d", "user"], capture_output=True, text=True).stdout.split()
    profiles = {"adhoc": f"match AdHoc {bundle}", "appstore": f"match AppStore {bundle}"}
    try:
        run("security", "create-keychain", "-p", keychain_password, str(keychain))
        run("security", "set-keychain-settings", "-lut", "3600", str(keychain))
        run("security", "unlock-keychain", "-p", keychain_password, str(keychain))
        run("security", "list-keychains", "-d", "user", "-s", str(keychain), *[s.strip('"') for s in searched])
        for kind in ("adhoc", "appstore"):
            run("fastlane", "match", kind, "--readonly", "--storage_mode", "s3", "--s3_bucket", bucket,
                "--s3_region", REGION, "--s3_object_prefix", "match/",
                "--app_identifier", bundle, "--team_id", team, "--keychain_name", str(keychain),
                "--keychain_password", keychain_password, cwd=source)
        (source / "ios/Flutter/Signing.xcconfig").write_text(
            f"CODE_SIGN_STYLE = Manual\nDEVELOPMENT_TEAM = {team}\nCODE_SIGN_IDENTITY = Apple Distribution\n"
            f"PROVISIONING_PROFILE_SPECIFIER = {profiles['adhoc']}\n")
        for kind, method in (("adhoc", "release-testing"), ("appstore", "app-store-connect")):
            (source / f"ios/ExportOptions-{kind}.plist").write_bytes(plistlib.dumps({
                "method": method, "teamID": team, "signingStyle": "manual", "signingCertificate": "Apple Distribution",
                "provisioningProfiles": {bundle: profiles[kind]}, "manageAppVersionAndBuildNumber": False}))
        mobile_flutter().native(["build", "ipa", "--release", "--build-name", version, "--build-number", str(number),
                                 *dart_defines(version, number, url),
                                 "--export-options-plist", "ios/ExportOptions-adhoc.plist"], cwd=source)
        shutil.copyfile(next((source / "build/ios/ipa").glob("*.ipa")), out / files["adhoc"])
        export = source / "build/ios/ipa-appstore"
        run("xcodebuild", "-exportArchive", "-archivePath", str(source / "build/ios/archive/Runner.xcarchive"),
            "-exportOptionsPlist", str(source / "ios/ExportOptions-appstore.plist"), "-exportPath", str(export))
        shutil.copyfile(next(export.glob("*.ipa")), out / files["appstore"])
    finally:
        subprocess.run(["security", "list-keychains", "-d", "user", "-s", *[s.strip('"') for s in searched]])
        subprocess.run(["security", "delete-keychain", str(keychain)], capture_output=True)
        for leftover in ("ios/Flutter/Signing.xcconfig", "ios/ExportOptions-adhoc.plist", "ios/ExportOptions-appstore.plist"):
            (source / leftover).unlink(missing_ok=True)
        print("Signing keychain deleted")
    return {kind: {"method": method, "identity": f"Apple Distribution ({team})", "profile": profiles[kind],
                   "file": files[kind]} for kind, method in (("adhoc", "ad-hoc"), ("appstore", "app-store-connect"))}


# --- The package ------------------------------------------------------------


def write_zip(source, destination, prefix=""):
    """Sorted entries with a fixed time, so the same files give the same zip."""
    root = Path(source)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        start = root / prefix if prefix else root
        for path in sorted(start.rglob("*")):
            if path.is_file() and not path.is_symlink():
                info = zipfile.ZipInfo(path.relative_to(root).as_posix(), ZIP_TIME)
                info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())


def assemble(version, number, jira_key, commit, platforms):
    """Check every part, then write the zip and build-info.json."""
    files = names(version, number)
    wanted = {"ios": [("ios", files["adhoc"]), ("ios", files["appstore"]), ("ios", "ios-signing.json")],
              "android": [("android", files["aab"]), ("android", "android-signing.json")]}
    contents, checks = {}, {}
    for platform in platforms:
        for folder, name in wanted[platform]:
            path = PARTS / folder / name
            if not path.exists():
                path = PARTS / name  # downloaded artifacts arrive flat
            if not path.exists():
                fail(f"The {platform} part has no {name}. Build it first, or run the release again.")
            contents[f"{folder}/{name}"] = path
    if "ios" in platforms:
        for kind, method in (("adhoc", ("ad-hoc",)), ("appstore", ("app-store-connect",))):
            info = mobile.ipa_info(contents[f"ios/{files[kind]}"].read_bytes())
            if (info["version"], info["build"]) != (version, str(number)) or info["method"] not in method or not info["signed"]:
                fail(f"{files[kind]} is {info['version']} ({info['build']}), {info['method']}, signed {info['signed']}: "
                     f"expected {version} ({number}), {method[0]}, signed.")
            checks[f"ios_{kind}"] = {"bundle_id": info["bundle_id"], "method": info["method"]}
    if "android" in platforms:
        info = mobile.aab_info(contents[f"android/{files['aab']}"].read_bytes())
        if info["version_code"] != int(number) or info["version_name"] != version:
            fail(f"{files['aab']} is {info['version_name']} ({info['version_code']}), expected {version} ({number}).")
        checks["android"] = {"package": info["package"], "target_sdk": info["target_sdk"],
                             "signed": info["signed"], "upload_certificate_sha256": info["upload_certificate_sha256"]}

    DIST.mkdir(parents=True, exist_ok=True)
    package = DIST / f"{SERVICE}-{version}.zip"
    sums = {name: mobile.file_sha256(path) for name, path in sorted(contents.items()) if not name.endswith(".json")}
    signing = {}
    for name, path in contents.items():
        if name.endswith("-signing.json"):
            signing[name.split("/")[0]] = json.loads(path.read_text())
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp)
        for name, path in contents.items():
            (staging / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, staging / name)
        (staging / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in sums.items()))
        shutil.copyfile(mobile.config()["dir"] / "pubspec.lock", staging / "pubspec.lock")
        write_json(staging / "build_info.json", {"service": SERVICE, "version": version, "build": str(number),
                                                 "commit": commit})
        write_zip(staging, package)
    info = {
        "service": SERVICE, "version": version, "commit": commit, "jira_key": jira_key,
        "build_url": os.environ.get("BUILD_URL"), "package": package.name,
        "sha256": mobile.file_sha256(package), "size_bytes": package.stat().st_size,
        "store_builds": {"ios": str(number) if "ios" in platforms else None,
                         "android": int(number) if "android" in platforms else None},
        "files": sums, "checks": checks, "signing": signing, "standins": sorted(mobile.standins()),
    }
    write_json(DIST / "build-info.json", info)
    print(f"Built {shown(package)}")
    for name, digest in sums.items():
        print(f"  {digest}  {name}")
    print(f"sha256 {info['sha256']}")


def shown(path):
    """A path from the repository root where it is inside it."""
    path = Path(path)
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def mobile_flutter():
    import flutter

    return flutter


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", choices=[SERVICE])
    parser.add_argument("--jira-key", default=None)
    parser.add_argument("--build-number", default=os.environ.get("BUILD_NUMBER"))
    part = parser.add_mutually_exclusive_group()
    part.add_argument("--platform", choices=["ios", "android"], help="build one part (the job store-floor-build)")
    part.add_argument("--verify-android", metavar="AAB", help="check a signed bundle")
    part.add_argument("--from-parts", action="store_true", help="assemble the parts already in dist/store-floor/parts")
    parser.add_argument("--runner-app", type=Path, help="with --platform ios: package this Runner.app (tests)")
    args = parser.parse_args()

    version = version_of(mobile.config()["dir"])
    number = args.build_number
    if args.verify_android:
        verify_android(args.verify_android, version, number)
        return
    if not number or not str(number).replace(".", "").isdigit():
        fail("A store build number is needed: Jenkins' BUILD_NUMBER, or --build-number.")
    if args.platform == "android":
        build_android(version, number, orders_url())
        return
    if args.platform == "ios":
        build_ios(version, number, orders_url(), args.runner_app)
        return

    commit = current_commit()
    if os.environ.get("JOB_NAME") == "ci":
        print("CI: Android only, unsigned. Releases build and sign both platforms.")
        build_android(version, number, "")
        unsigned = PARTS / "android" / "android-signing.json"
        write_json(unsigned, {"signed_by": None, "note": "CI build, not signed"})
        assemble_unsigned(version, number, args.jira_key, commit)
        return
    if not args.from_parts:
        shutil.rmtree(PARTS, ignore_errors=True)
        import jenkins

        jenkins.build(version, number, commit, ["ios", "android"], orders_url())
    assemble(version, number, args.jira_key, commit, ["ios", "android"])


def assemble_unsigned(version, number, jira_key, commit):
    """CI's package: the unsigned bundle, so the later stages and the scan run."""
    files = names(version, number)
    DIST.mkdir(parents=True, exist_ok=True)
    package = DIST / f"{SERVICE}-{version}.zip"
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp)
        (staging / "android").mkdir()
        shutil.copyfile(PARTS / "android" / files["aab"], staging / "android" / files["aab"])
        shutil.copyfile(mobile.config()["dir"] / "pubspec.lock", staging / "pubspec.lock")
        write_zip(staging, package)
    write_json(DIST / "build-info.json", {
        "service": SERVICE, "version": version, "commit": commit, "jira_key": jira_key,
        "build_url": os.environ.get("BUILD_URL"), "package": package.name, "sha256": mobile.file_sha256(package),
        "size_bytes": package.stat().st_size, "store_builds": {"ios": None, "android": None}, "signed": False})
    print(f"Built {shown(package)} (CI, unsigned)")


if __name__ == "__main__":
    main()
