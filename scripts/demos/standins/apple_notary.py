"""Apple's notary service, real or stand-in, behind one interface.

    notary = apple_notary.connect(standin=True)               # or standin=False with an API key
    result = notary.submit_and_wait(pkg)                       # {"id", "status", "log"}
    notary.staple(pkg)
    accepted, source, text = notary.assess(pkg)                # Gatekeeper's verdict

The real one runs `xcrun notarytool submit --wait` with an App Store Connect
API key, `xcrun stapler` and `spctl --assess --type install`. The stand-in
answers like it: it uploads nothing, takes a few minutes (STANDIN_NOTARY_SECONDS,
150 by default), and really inspects the package, so it rejects what Apple
would reject for the reasons Apple gives: an executable without the hardened
runtime, a signature without a secure timestamp, or no Developer ID signature.
Its log has Apple's format. It says once, when it starts, that it is the
stand-in; the rest of its output reads like notarytool's.

Standard library only, Python 3.9: it runs with the macOS system Python on
the Mac agent.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

DOCS = "https://developer.apple.com/documentation/security/resolving-common-notarization-issues"


def log(line=""):
    print(line, flush=True)


def connect(standin, key_path=None, key_id=None, issuer=None, seconds=None):
    if standin:
        return StandinNotary(seconds)
    if not (key_path and key_id and issuer):
        raise SystemExit("Notarization needs the App Store Connect API key: APPLE_NOTARY_KEY, APPLE_NOTARY_KEY_ID and "
                         "APPLE_NOTARY_ISSUER. Or set STANDIN=1 for the stand-in.")
    return AppleNotary(key_path, key_id, issuer)


def run(command, check=True):
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"{' '.join(command[:3])} failed ({result.returncode}): {(result.stderr or result.stdout).strip()[:500]}")
    return result


class AppleNotary:
    """notarytool with an App Store Connect API key (a .p8 file), stapler and spctl."""

    standin = False

    def __init__(self, key_path, key_id, issuer):
        self.auth = ["--key", str(key_path), "--key-id", key_id, "--issuer", issuer]

    def submit_and_wait(self, pkg, timeout_minutes=30):
        log(f"Submitting {Path(pkg).name} to the Apple notary service and waiting for the result")
        result = run(["xcrun", "notarytool", "submit", str(pkg), *self.auth, "--wait",
                      "--timeout", f"{timeout_minutes}m", "--output-format", "json"], check=False)
        try:
            reply = json.loads(result.stdout)
        except ValueError:
            raise SystemExit(f"notarytool gave no result: {(result.stderr or result.stdout).strip()[:500]}")
        submission = reply.get("id")
        log(f"  id: {submission}\n  status: {reply.get('status')}")
        notary_log = {}
        if submission:
            with tempfile.TemporaryDirectory() as temp:
                out = Path(temp) / "log.json"
                if run(["xcrun", "notarytool", "log", submission, *self.auth, str(out)], check=False).returncode == 0:
                    notary_log = json.loads(out.read_text())
        return {"id": submission, "status": reply.get("status"), "log": notary_log}

    def staple(self, pkg):
        log(run(["xcrun", "stapler", "staple", str(pkg)]).stdout.strip())
        log(run(["xcrun", "stapler", "validate", str(pkg)]).stdout.strip())

    def assess(self, pkg):
        result = run(["spctl", "--assess", "--type", "install", "--verbose=4", str(pkg)], check=False)
        text = (result.stderr + result.stdout).strip()
        source = re.search(r"source=(.+)", text)
        return result.returncode == 0, source.group(1).strip() if source else "", text


class StandinNotary:
    """Answers like Apple's notary service, from a real inspection of the package."""

    standin = True

    def __init__(self, seconds=None):
        self.seconds = int(seconds if seconds is not None else os.environ.get("STANDIN_NOTARY_SECONDS", "150"))
        self.tickets = {}
        print(f"notary stand-in started ({self.seconds}s per submission)", file=sys.stderr, flush=True)

    def submit_and_wait(self, pkg, timeout_minutes=30):
        pkg = Path(pkg)
        submission = str(uuid.uuid4())
        size = pkg.stat().st_size
        log(f"Conducting pre-submission checks for {pkg.name} and initiating connection to the Apple notary service...")
        time.sleep(min(3, self.seconds))
        log(f"Submission ID received\n  id: {submission}")
        log(f"Upload progress: 100.00% ({size / 1e6:.1f} MB of {size / 1e6:.1f} MB)")
        log(f"Successfully uploaded file\n  id: {submission}\n  path: {pkg}")
        log("Waiting for processing to complete.")
        uploaded = datetime.datetime.now(datetime.timezone.utc)
        issues, tickets = inspect_package(pkg)
        deadline = time.monotonic() + max(0, self.seconds - 3)
        while time.monotonic() < deadline:
            time.sleep(min(15, max(0.0, deadline - time.monotonic())))
            log("Current status: In Progress")
        status = "Invalid" if issues else "Accepted"
        log(f"Current status: {status}\nProcessing complete\n  id: {submission}\n  status: {status}")
        notary_log = {
            "logFormatVersion": 1,
            "jobId": submission,
            "status": status,
            "statusSummary": "Archive contains critical validation errors" if issues else "Ready for distribution",
            "statusCode": 4000 if issues else 0,
            "archiveFilename": pkg.name,
            "uploadDate": uploaded.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "sha256": sha256(pkg),
            "ticketContents": None if issues else tickets,
            "issues": issues or None,
        }
        if not issues:
            self.tickets[str(pkg)] = notary_log
        return {"id": submission, "status": status, "log": notary_log}

    def staple(self, pkg):
        if str(pkg) not in self.tickets:
            raise SystemExit(f"Could not validate ticket for {pkg}: no accepted notarization")
        log(f"Processing: {pkg}\nThe staple and validate action worked!")

    def assess(self, pkg):
        if str(pkg) not in self.tickets:
            return False, "Unnotarized Developer ID", f"{pkg}: rejected\nsource=Unnotarized Developer ID"
        return True, "Notarized Developer ID", f"{pkg}: accepted\nsource=Notarized Developer ID"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_package(pkg):
    """The issues Apple would report for a .pkg, and the ticket entries if none.
    Checks every Mach-O file of every app in the payload."""
    issues, tickets = [], []
    work = Path(tempfile.mkdtemp(prefix="notary-"))
    try:
        expanded = work / "expanded"
        run(["pkgutil", "--expand-full", str(pkg), str(expanded)])
        for binary in sorted(macho_files(expanded)):
            relative = f"{pkg.name}/{binary.relative_to(expanded).as_posix()}"
            details = run(["codesign", "--display", "--verbose=4", str(binary)], check=False)
            text = details.stdout + details.stderr
            architectures = run(["lipo", "-archs", str(binary)], check=False).stdout.split() or ["arm64"]
            problems = []
            if "code object is not signed" in text or details.returncode != 0:
                problems.append("The binary is not signed.")
            else:
                if "Signature=adhoc" in text or "Authority=Developer ID Application:" not in text:
                    problems.append("The binary is not signed with a valid Developer ID certificate.")
                if "Timestamp=" not in text:
                    problems.append("The signature does not include a secure timestamp.")
                flags = re.search(r"flags=0x[0-9a-f]+\(([^)]*)\)", text)
                if not flags or "runtime" not in flags.group(1):
                    problems.append("The executable does not have the hardened runtime enabled.")
                if run(["codesign", "--verify", "--strict", str(binary)], check=False).returncode != 0:
                    problems.append("The signature of the binary is invalid.")
            for problem in problems:
                for arch in architectures:
                    issues.append({"severity": "error", "code": None, "path": relative, "message": problem,
                                   "docUrl": DOCS, "architecture": arch})
            if not problems:
                cdhash = re.search(r"CDHash=([0-9a-f]+)", text)
                for arch in architectures:
                    tickets.append({"path": relative, "digestAlgorithm": "SHA-256",
                                    "cdhash": cdhash.group(1) if cdhash else "", "arch": arch})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return issues, tickets


def macho_files(root):
    magics = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 4:
            with open(path, "rb") as file:
                if file.read(4) in magics:
                    yield path


if __name__ == "__main__":
    # python3 apple_notary.py <pkg>: notarize with the stand-in, for a quick look.
    result = StandinNotary(int(os.environ.get("STANDIN_NOTARY_SECONDS", "5"))).submit_and_wait(Path(sys.argv[1]))
    print(json.dumps(result["log"], indent=2))
