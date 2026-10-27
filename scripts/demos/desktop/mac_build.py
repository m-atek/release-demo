"""The Mac track of store-assistant, on the Mac agent: test, build, sign, package, notarize.

    python3 scripts/demos/desktop/mac_build.py test store-assistant
    python3 scripts/demos/desktop/mac_build.py build store-assistant --jira-key ORD-71

test runs the Swift tests and writes dist/<service>/parts/mac/reports/: the
JUnit report and the coverage in SonarQube's generic format. build writes the
signed, notarized and stapled StoreAssistant-<version>.pkg and mac-build.json
to dist/<service>/parts/mac/, which the pipeline stashes for the tools host.

Build steps:
1. xcodebuild builds the app for arm64 and x86_64. Without Xcode (only the
   Command Line Tools), swift build builds each architecture and lipo joins them.
2. The app bundle is signed with Developer ID Application, hardened runtime
   and a secure timestamp, from a keychain made for this build only, with a
   random password. The keychain is deleted at the end, also when the build
   fails or is aborted. The .p12 files and their passphrases come from SSM
   through Jenkins credentials: APPLE_APP_P12 and APPLE_INSTALLER_P12 (paths),
   APPLE_APP_P12_PASSWORD and APPLE_INSTALLER_P12_PASSWORD.
3. pkgbuild and productbuild make the .pkg, signed with Developer ID Installer.
   BundleIsVersionChecked is false, so a rollback can install an older app
   over a newer one.
4. notarytool notarizes it with the App Store Connect API key (APPLE_NOTARY_KEY,
   a path, APPLE_NOTARY_KEY_ID, APPLE_NOTARY_ISSUER), then stapler staples the
   ticket and spctl --assess --type install checks it.

With STANDIN=1 (services.yaml settings, or the environment) the Apple
Developer Program is a stand-in: the identity is a throwaway self-signed
"Developer ID Application" certificate made inside the build keychain, so the
app is really signed, with Apple's real secure timestamp; productbuild only
signs with an installer identity Apple issued, so the package itself stays
unsigned; and scripts/demos/standins/apple_notary.py stands in for the notary
service, staple and Gatekeeper. The startup log says so.

Standard library only, Python 3.9: it runs with the macOS system Python.
"""

import argparse
import atexit
import datetime
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent / "standins"))
import apple_notary  # noqa: E402

OPENSSL = "/usr/bin/openssl"  # LibreSSL: its .p12 files are the kind `security import` reads
MIN_XCODE = 26


def log(line=""):
    print(line, flush=True)


def run(command, cwd=None, check=True, quiet=False, env=None):
    """Run a command, printing it (without secrets: callers pass `shown`)."""
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise SystemExit(f"{command[0]} {command[1] if len(command) > 1 else ''} failed ({result.returncode}):\n{output[-3000:]}")
    if not quiet and result.stdout.strip():
        log(result.stdout.rstrip())
    return result


def setting(service, name, default):
    """A value from the service's settings in services.yaml, read without PyYAML,
    which the Mac agent's system Python doesn't have. The environment wins."""
    if os.environ.get(name) is not None:
        return os.environ[name]
    text = (ROOT / "services.yaml").read_text()
    block = re.search(rf"^{re.escape(service)}:\n((?:[ #].*\n|\n)*)", text, re.M)
    match = re.search(rf"^\s+{name}:\s*\"?([^\"\n#]*)\"?", block.group(1), re.M) if block else None
    return match.group(1).strip() if match else default


def standin_mode(service):
    """STANDIN_APPLE or STANDIN: 1 for the Apple stand-ins, 0 for Apple's own services."""
    value = setting(service, "STANDIN_APPLE", None)
    value = setting(service, "STANDIN", "1") if value is None else value
    return value not in ("0", "false", "no", "")


def service_dir(service):
    return ROOT / "services" / service


def parts_dir(service):
    return ROOT / "dist" / service / "parts" / "mac"


def app_config(service):
    return json.loads((service_dir(service) / "mac" / "app.json").read_text())


def version_of(service):
    return (service_dir(service) / "VERSION").read_text().strip()


def xcode():
    """The Xcode version, or None when only the Command Line Tools are installed."""
    developer = run(["xcode-select", "-p"], check=False, quiet=True).stdout.strip()
    if not developer or "CommandLineTools" in developer:
        return None
    result = run(["xcodebuild", "-version"], check=False, quiet=True)
    match = re.search(r"Xcode (\d+)(\.\d+)*", result.stdout)
    if match and run(["xcodebuild", "-checkFirstLaunchStatus"], check=False, quiet=True).returncode != 0:
        raise SystemExit(f"Xcode {match.group(0).split()[1]} hasn't finished its first launch: "
                         "run sudo xcodebuild -runFirstLaunch on the Mac agent.")
    return match.group(0).split()[1] if match else None


# Test

def test(service):
    package = service_dir(service) / "mac"
    reports = parts_dir(service) / "reports"
    shutil.rmtree(reports, ignore_errors=True)
    reports.mkdir(parents=True)
    version = xcode()
    work = Path(tempfile.mkdtemp(prefix="store-assistant-test-"))
    try:
        if version:
            log(f"Testing with xcodebuild (Xcode {version})")
            scheme = test_scheme(package)
            bundle = work / "tests.xcresult"
            result = run(["xcodebuild", "test", "-scheme", scheme, "-destination", "platform=macOS",
                          "-derivedDataPath", str(work / "derived"), "-resultBundlePath", str(bundle),
                          "-enableCodeCoverage", "YES"], cwd=package, check=False, quiet=True)
            log(result.stdout[-4000:])
            cases = xcresult_cases(bundle)
            coverage = xccov_lines(bundle)
        else:
            log("Testing with swift test: Xcode isn't installed, only the Command Line Tools")
            result = run(["swift", "test", "--enable-code-coverage", "--scratch-path", str(work / "build")],
                         cwd=package, check=False, quiet=True)
            output = result.stdout + "\n" + result.stderr
            log("\n".join(line for line in output.splitlines()
                          if re.search(r"Test |error:|Build complete", line))[-6000:])
            cases = swift_testing_cases(output)
            coverage = lcov_lines(package, work / "build")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_junit(reports / "junit-mac.xml", "StoreAssistant (macOS)", cases)
    write_generic_coverage(reports / "coverage-mac.xml", coverage)
    failed = [c for c in cases if c["result"] != "passed"]
    log(f"{len(cases)} tests, {len(failed)} failed")
    if result.returncode != 0 or failed or not cases:
        raise SystemExit("The Mac tests failed." if cases else "The Mac tests didn't run.")


def test_scheme(package):
    listing = json.loads(run(["xcodebuild", "-list", "-json"], cwd=package, quiet=True).stdout)
    schemes = listing.get("workspace", listing.get("project", {})).get("schemes", [])
    for scheme in schemes:
        if scheme.endswith("-Package"):
            return scheme
    return "StoreAssistant"


def swift_testing_cases(output):
    """Swift Testing's console output, as test cases: the Command Line Tools'
    swift test writes no xUnit file for Swift Testing."""
    cases = {}
    for line in output.splitlines():
        match = re.search(r"Test (\w+)\(\) (passed|failed) after ([\d.]+) seconds", line)
        if match:
            cases[match.group(1)] = {"name": match.group(1), "result": match.group(2),
                                     "seconds": float(match.group(3)), "message": cases.get(match.group(1), {}).get("message")}
            continue
        issue = re.search(r"Test (\w+)\(\) recorded an issue at (.+)", line)
        if issue:
            cases.setdefault(issue.group(1), {"name": issue.group(1), "result": "failed", "seconds": 0.0})
            cases[issue.group(1)]["message"] = issue.group(2).strip()
    return list(cases.values())


def xcresult_cases(bundle):
    if not bundle.exists():
        return []
    reply = run(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(bundle)], check=False, quiet=True)
    try:
        nodes = json.loads(reply.stdout).get("testNodes", [])
    except ValueError:
        return []
    cases = []

    def walk(node):
        if node.get("nodeType") == "Test Case":
            messages = [c.get("name", "") for c in node.get("children", []) if c.get("nodeType") == "Failure Message"]
            seconds = float(re.sub(r"[^\d.]", "", node.get("duration", "0") or "0") or 0)
            cases.append({"name": node.get("name", "").rstrip("()"), "seconds": seconds,
                          "result": "passed" if node.get("result") == "Passed" else "failed",
                          "message": "\n".join(messages) or None})
        for child in node.get("children", []):
            walk(child)

    for node in nodes:
        walk(node)
    return cases


def write_junit(path, suite, cases):
    from xml.sax.saxutils import quoteattr, escape
    failures = sum(c["result"] != "passed" for c in cases)
    rows = [f'<testsuites><testsuite name={quoteattr(suite)} tests="{len(cases)}" failures="{failures}" '
            f'time="{sum(c["seconds"] for c in cases):.3f}">']
    for case in cases:
        rows.append(f'<testcase classname={quoteattr(suite)} name={quoteattr(case["name"])} time="{case["seconds"]:.3f}">')
        if case["result"] != "passed":
            rows.append(f'<failure message={quoteattr(case.get("message") or "failed")}>{escape(case.get("message") or "")}</failure>')
        rows.append("</testcase>")
    rows.append("</testsuite></testsuites>")
    path.write_text("\n".join(rows) + "\n")


def repository_path(path):
    """A source path relative to the repository, as SonarQube on the tools host sees it."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def xccov_lines(bundle):
    reply = run(["xcrun", "xccov", "view", "--archive", "--json", str(bundle)], check=False, quiet=True)
    try:
        archive = json.loads(reply.stdout)
    except ValueError:
        return {}
    coverage = {}
    for file, lines in archive.items():
        relative = repository_path(file)
        if relative and "/Sources/" in relative:
            coverage[relative] = {l["line"]: l.get("executionCount", 0) for l in lines if l.get("isExecutable")}
    return coverage


def lcov_lines(package, build):
    """Line coverage from swift test's profile, through llvm-cov's lcov export."""
    debug = build / "debug"
    profile = debug / "codecov" / "default.profdata"
    binaries = list(debug.glob("*.xctest/Contents/MacOS/*"))
    if not profile.exists() or not binaries:
        return {}
    reply = run(["xcrun", "llvm-cov", "export", "-format=lcov", "-instr-profile", str(profile), str(binaries[0])],
                check=False, quiet=True)
    coverage, current = {}, None
    for line in reply.stdout.splitlines():
        if line.startswith("SF:"):
            relative = repository_path(line[3:])
            current = coverage.setdefault(relative, {}) if relative and "/Sources/" in relative else None
        elif line.startswith("DA:") and current is not None:
            number, count = line[3:].split(",")[:2]
            current[int(number)] = int(count)
    return coverage


def write_generic_coverage(path, coverage):
    from xml.sax.saxutils import quoteattr
    rows = ['<coverage version="1">']
    for file, lines in sorted(coverage.items()):
        rows.append(f"  <file path={quoteattr(file)}>")
        rows += [f'    <lineToCover lineNumber="{n}" covered="{"true" if c > 0 else "false"}"/>' for n, c in sorted(lines.items())]
        rows.append("  </file>")
    rows.append("</coverage>")
    path.write_text("\n".join(rows) + "\n")


# Build

def compile_app(package, config, work):
    """The universal executable: xcodebuild, or swift build per architecture and lipo."""
    executable = config["executable"]
    version = xcode()
    if version:
        if int(version.split(".")[0]) < MIN_XCODE:
            log(f"Warning: Xcode {version} is older than Xcode {MIN_XCODE}, which the demo expects")
        log(f"Building with xcodebuild (Xcode {version}) for arm64 and x86_64")
        derived = work / "derived"
        run(["xcodebuild", "build", "-scheme", executable, "-configuration", "Release",
             "-destination", "generic/platform=macOS", "-derivedDataPath", str(derived),
             "ARCHS=arm64 x86_64", "ONLY_ACTIVE_ARCH=NO"], cwd=package, quiet=True)
        return derived / "Build" / "Products" / "Release" / executable, f"xcodebuild (Xcode {version})"
    swift = run(["swift", "--version"], quiet=True).stdout.splitlines()[0].strip()
    log(f"Building with swift build for arm64 and x86_64: Xcode isn't installed, only the Command Line Tools ({swift})")
    slices = []
    for arch in ("arm64", "x86_64"):
        scratch = work / f"build-{arch}"
        run(["swift", "build", "-c", "release", "--product", executable, "--scratch-path", str(scratch),
             "--triple", f"{arch}-apple-macosx{config['minimum_macos']}"], cwd=package, quiet=True)
        slices.append(str(next(scratch.glob(f"*/release/{executable}"))))
    universal = work / executable
    run(["lipo", "-create", *slices, "-output", str(universal)], quiet=True)
    return universal, swift


def assemble_bundle(config, executable, version, build_number, root):
    """Applications/<name>.app with its Info.plist, under the package root."""
    app = root / "Applications" / f"{config['name']}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    shutil.copy2(executable, app / "Contents" / "MacOS" / config["executable"])
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": config["name"],
        "CFBundleExecutable": config["executable"],
        "CFBundleIdentifier": config["bundle_id"],
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": config["name"],
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": build_number,
        "LSApplicationCategoryType": "public.app-category.business",
        "LSMinimumSystemVersion": config["minimum_macos"],
        "NSHumanReadableCopyright": config["copyright"],
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
    }
    with open(app / "Contents" / "Info.plist", "wb") as file:
        plistlib.dump(info, file)
    (app / "Contents" / "PkgInfo").write_text("APPL????")
    return app


class BuildKeychain:
    """A keychain for this build only: random password, first in the search
    list while the build runs, deleted at the end whatever happens."""

    def __init__(self, work):
        self.build = os.environ.get("BUILD_NUMBER", "local")
        self.path = work / f"store-assistant-build-{self.build}-{secrets.token_hex(3)}.keychain-db"
        self.password = secrets.token_hex(24)
        self.original = None
        self.deleted = False

    def __enter__(self):
        atexit.register(self.delete)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self._on_signal)
        self.original = [p.strip().strip('"') for p in
                         run(["security", "list-keychains", "-d", "user"], quiet=True).stdout.splitlines() if p.strip()]
        run(["security", "create-keychain", "-p", self.password, str(self.path)], quiet=True)
        run(["security", "set-keychain-settings", "-lut", "3600", str(self.path)], quiet=True)
        run(["security", "unlock-keychain", "-p", self.password, str(self.path)], quiet=True)
        run(["security", "list-keychains", "-d", "user", "-s", str(self.path), *self.original], quiet=True)
        log(f"Keychain {self.path.name} created for build #{self.build} only, with a random password")
        return self

    def __exit__(self, *exc):
        self.delete()

    def _on_signal(self, signum, frame):
        self.delete()
        raise SystemExit(f"Stopped by signal {signum}; the signing keychain is deleted.")

    def import_p12(self, p12, passphrase):
        run(["security", "import", str(p12), "-k", str(self.path), "-P", passphrase, "-f", "pkcs12",
             "-T", "/usr/bin/codesign", "-T", "/usr/bin/productbuild", "-T", "/usr/bin/productsign"], quiet=True)
        # Lets codesign and productbuild use the key without a dialog.
        run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s",
             "-k", self.password, str(self.path)], quiet=True)

    def identity(self, prefix):
        """The SHA-1 and name of the first identity whose name starts with prefix."""
        listing = run(["security", "find-identity", str(self.path)], quiet=True).stdout
        for sha1, name in re.findall(r'\) ([0-9A-F]{40}) "([^"]+)"', listing):
            if name.startswith(prefix):
                return sha1, name
        return None, None

    def delete(self):
        if self.deleted:
            return
        self.deleted = True
        if self.original is not None:
            subprocess.run(["security", "list-keychains", "-d", "user", "-s", *self.original], capture_output=True)
        if self.path.exists():
            subprocess.run(["security", "delete-keychain", str(self.path)], capture_output=True)
        log(f"Keychain {self.path.name} deleted")


def standin_identity(work, common_name, extended_key_usage):
    """A throwaway self-signed identity as a .p12 with a random passphrase.
    The key never leaves the build's private temporary folder."""
    folder = work / "identity"
    folder.mkdir(mode=0o700, exist_ok=True)
    config = folder / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name = dn\nprompt = no\n[dn]\n"
                      f"CN = {common_name}\nOU = STANDIN01\nO = Stand-in Developer\nC = NL\n"
                      "[ext]\nbasicConstraints = critical,CA:false\nkeyUsage = critical,digitalSignature\n"
                      f"extendedKeyUsage = critical,{extended_key_usage}\n")
    key, cert, p12 = folder / "key.pem", folder / "cert.pem", folder / "identity.p12"
    passphrase = secrets.token_hex(16)
    run([OPENSSL, "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "2", "-config", str(config), "-extensions", "ext"], quiet=True)
    run([OPENSSL, "pkcs12", "-export", "-inkey", str(key), "-in", str(cert), "-out", str(p12),
         "-passout", f"pass:{passphrase}", "-name", common_name], quiet=True)
    key.unlink()
    return p12, passphrase


def real_identity(variable):
    path, passphrase = os.environ.get(variable), os.environ.get(f"{variable}_PASSWORD")
    if not path or not Path(path).is_file() or passphrase is None:
        raise SystemExit(f"{variable} and {variable}_PASSWORD aren't set: the Jenkins credentials from SSM "
                         "(desktop/apple/...). Or set STANDIN=1 for the stand-in.")
    return Path(path), passphrase


def sign_app(keychain, app, standin, work):
    log("Developer ID Application signing")
    if standin:
        p12, passphrase = standin_identity(work, "Developer ID Application: Stand-in Developer (STANDIN01)", "codeSigning")
        keychain.import_p12(p12, passphrase)
        p12.unlink()
    else:
        keychain.import_p12(*real_identity("APPLE_APP_P12"))
    sha1, name = keychain.identity("Developer ID Application:")
    if not sha1:
        raise SystemExit("No Developer ID Application identity in the build keychain.")
    entitlements = work / "entitlements.plist"
    with open(entitlements, "wb") as file:
        plistlib.dump({}, file)  # hardened runtime, no exceptions
    run(["codesign", "--force", "--options", "runtime", "--timestamp", "--entitlements", str(entitlements),
         "--keychain", str(keychain.path), "--sign", sha1, str(app)], quiet=True)
    run(["codesign", "--verify", "--strict", "--deep", "--verbose=2", str(app)], quiet=True)
    details = run(["codesign", "--display", "--verbose=4", str(app)], quiet=True).stderr
    signature = {
        "authority": name,
        "team_id": (re.search(r"TeamIdentifier=(.+)", details) or [None, ""])[1].strip(),
        "timestamp": (re.search(r"Timestamp=(.+)", details) or [None, ""])[1].strip(),
        "hardened_runtime": "(runtime)" in details,
        "cdhash": (re.search(r"CDHash=([0-9a-f]+)", details) or [None, ""])[1],
    }
    log(f"  signed by {name}, hardened runtime, timestamp {signature['timestamp']}")
    return signature


def build_pkg(keychain, root, scripts, config, version, out, standin, work):
    """pkgbuild for the component, productbuild for the signed product."""
    component_plist = work / "component.plist"
    run(["pkgbuild", "--analyze", "--root", str(root), str(component_plist)], quiet=True)
    with open(component_plist, "rb") as file:
        components = plistlib.load(file)
    for component in components:
        component["BundleIsVersionChecked"] = False  # a rollback may put an older app over a newer one
        component["BundleIsRelocatable"] = False      # always /Applications, never a copy elsewhere
    with open(component_plist, "wb") as file:
        plistlib.dump(components, file)
    component = work / "component.pkg"
    run(["pkgbuild", "--root", str(root), "--component-plist", str(component_plist), "--scripts", str(scripts),
         "--identifier", config["bundle_id"], "--version", version, "--install-location", "/", str(component)], quiet=True)
    pkg = out / f"{config['package_prefix']}-{version}.pkg"
    log("Developer ID Installer signing")
    if standin:
        # productbuild signs only with an installer identity Apple issued.
        run(["productbuild", "--package", str(component), str(pkg)], quiet=True)
        return pkg, {"signed": False, "authority": None}
    keychain.import_p12(*real_identity("APPLE_INSTALLER_P12"))
    sha1, name = keychain.identity("Developer ID Installer:")
    if not sha1:
        raise SystemExit("No Developer ID Installer identity in the build keychain.")
    run(["productbuild", "--package", str(component), "--sign", sha1, "--keychain", str(keychain.path),
         "--timestamp", str(pkg)], quiet=True)
    check = run(["pkgutil", "--check-signature", str(pkg)], quiet=True).stdout
    log(check.rstrip())
    return pkg, {"signed": True, "authority": name}


def file_digest(path, algorithm):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(service, jira_key):
    config = app_config(service)
    version = version_of(service)
    build_number = os.environ.get("BUILD_NUMBER", "1")
    standin = standin_mode(service)
    os.environ.setdefault("STANDIN_NOTARY_SECONDS", setting(service, "STANDIN_NOTARY_SECONDS", "150"))
    out = parts_dir(service)
    for stale in out.glob("*.pkg"):
        stale.unlink()
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    log(f"Store Assistant {version} for macOS, build {build_number}{f', {jira_key}' if jira_key else ''}")
    if standin:
        log("STANDIN=1: Apple stand-ins for the Developer ID identity, notarization, stapling and Gatekeeper")

    work = Path(tempfile.mkdtemp(prefix="store-assistant-build-"))
    work.chmod(0o700)
    try:
        executable, toolchain = compile_app(service_dir(service) / "mac", config, work)
        architectures = run(["lipo", "-archs", str(executable)], quiet=True).stdout.split()
        root = work / "root"
        app = assemble_bundle(config, executable, version, build_number, root)
        with BuildKeychain(work) as keychain:
            app_signature = sign_app(keychain, app, standin, work)
            pkg, installer_signature = build_pkg(keychain, root, service_dir(service) / "mac" / "pkg-scripts",
                                                 config, version, out, standin, work)
        notary = apple_notary.connect(standin, os.environ.get("APPLE_NOTARY_KEY"),
                                      os.environ.get("APPLE_NOTARY_KEY_ID"), os.environ.get("APPLE_NOTARY_ISSUER"))
        log("Notarization")
        result = notary.submit_and_wait(pkg)
        (out / "notarization-log.json").write_text(json.dumps(result["log"], indent=2) + "\n")
        if result["status"] != "Accepted":
            for issue in (result["log"] or {}).get("issues") or []:
                log(f"  {issue.get('severity')}: {issue.get('path')}: {issue.get('message')}")
            raise SystemExit(f"Notarization {result['status']}: see notarization-log.json")
        log("Stapling")
        notary.staple(pkg)
        accepted, source, text = notary.assess(pkg)
        log(f"Gatekeeper: spctl --assess --type install\n{text}")
        if not accepted:
            raise SystemExit(f"Gatekeeper rejects the package: {source}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    info = {
        "service": service, "version": version, "build_number": build_number, "jira_key": jira_key,
        "bundle_id": config["bundle_id"], "toolchain": toolchain, "architectures": architectures,
        "app_signature": app_signature, "installer_signature": installer_signature,
        "notarization": {"id": result["id"], "status": result["status"], "standin": notary.standin},
        "stapled": True, "gatekeeper": {"accepted": accepted, "source": source, "standin": notary.standin},
        "standin": standin,
        "pkg": {"name": pkg.name, "size_bytes": pkg.stat().st_size,
                "sha256": file_digest(pkg, "sha256"), "sha512": file_digest(pkg, "sha512")},
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.monotonic() - started),
    }
    (out / "mac-build.json").write_text(json.dumps(info, indent=2) + "\n")
    log(f"Built {pkg.relative_to(ROOT)}: notarized ({source}), {info['seconds']}s")
    log(f"sha256 {info['pkg']['sha256']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["test", "build"])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    args = parser.parse_args()
    if args.command == "test":
        test(args.service)
    else:
        build(args.service, args.jira_key)


if __name__ == "__main__":
    main()
