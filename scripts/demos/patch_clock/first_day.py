"""The acceptance checks that need the real thing, for the first day on the tools host. None of them writes,
except what the checks say: check 1's temporary folders, and check 2 and check 5 only read and get refused.

    python scripts/demos/patch_clock/first_day.py cve         check 1: Grype's rating of the package, on that day's database
    python scripts/demos/patch_clock/first_day.py seed        check 2: 1.12.0's real package, once the seed has run
    python scripts/demos/patch_clock/first_day.py read-only   check 5: the role and the JFrog token get refused
    python scripts/demos/patch_clock/first_day.py rows        check 6: the rows with the SBOM swapped, missing, or the role denied

From the Mac each runs itself on the tools host. Exit 0 when the check passes.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import candidates  # noqa: E402
import host  # noqa: E402
import live as readers  # noqa: E402
import publish  # noqa: E402
import search  # noqa: E402
import seed  # noqa: E402
import tools  # noqa: E402
from common import ROOT, get_parameter, http_get, service, ssm_client  # noqa: E402

RUNTIME = "python3.13"
DECODER = "brotli==1.2.1"  # for reading the function's answer in check 2, never in the service


def line(ok, text):
    print(f"{'ok  ' if ok else 'FAIL'} {text}")
    return ok


# Check 1

def ids_of(candidate):
    return {candidate.cve.upper(), candidate.ghsa.upper()}


def matches_on(report, candidate):
    wanted = ids_of(candidate)
    return [match for match in report["matches"] if wanted & {name.upper() for name in (
        match["vulnerability"]["id"], *(item.get("id", "") for item in match.get("relatedVulnerabilities") or []))}]


def scanned(package, version):
    """pip with build.py's flags into a temporary folder, Syft, then Grype with --fail-on critical. (report, exit code)"""
    import build

    with tools.scratch() as work:
        (work / "requirements.txt").write_text(f"{package}=={version}\n")
        target = work / "package"
        target.mkdir()
        build.install_requirements(work / "requirements.txt", target, RUNTIME)
        tools.syft(target, work, "patch-clock-check")
        done = tools.grype(["sbom:/work/sbom.cdx.json", "--fail-on", "critical", "-o", "json"], mounts=[(work, "/work:ro")])
    return tools.json_of(done, "grype"), done.returncode


def stands(candidate):
    """The bad version: this CVE at severity Critical, exit non-zero. The good one: no match on it, exit 0."""
    report, code = scanned(candidate.package, candidate.bad)
    rated = {match["vulnerability"].get("severity") for match in matches_on(report, candidate)
             if match["artifact"]["version"] == candidate.bad}
    bad = line("Critical" in rated and code != 0,
               f"{candidate.package} {candidate.bad}: {candidate.cve} rated {sorted(rated) or 'nothing'}, exit {code}")
    report, code = scanned(candidate.package, candidate.good)
    held = matches_on(report, candidate)
    good = line(not held and code == 0, f"{candidate.package} {candidate.good}: {'no match on it' if not held else 'still matches'}, exit {code}")
    return bad and good


def check_cve():
    print(f"Grype's database: {tools.db_update() or 'updated'}; built {tools.db_status().get('built')}")
    order = [candidates.CHOSEN, *(name for name in candidates.CANDIDATES if name != candidates.CHOSEN)]
    for name in order:
        candidate = candidates.CANDIDATES[name]
        print(f"{candidate.cve}, {candidate.package} {candidate.bad} and {candidate.good}:")
        if stands(candidate):
            facts = search.read_facts(candidate.cve)
            print(f"The reply's head would read: {search.head(candidate.cve, facts, '@Sam')}")
            print(f"Its second line: {search.advisory_line(facts, [])}")
            if name != candidates.CHOSEN:
                print(f"The video changes CVE: .venv/bin/python scripts/demos/patch_clock/use_fallback.py {name}, and the script's fallback words.")
            return 0
    print("All three fail: stop and ask. The refusal is the video.")
    return 1


# Check 2

def decompressed(body, work):
    """The bytes the function sent, through Brotli off the function, in a folder of its own."""
    done = subprocess.run([sys.executable, "-c", "import sys, brotli; sys.stdout.buffer.write(brotli.decompress(sys.stdin.buffer.read()))"],
                          input=body, capture_output=True, env={"PYTHONPATH": str(work)}, check=True)
    return done.stdout


def check_seed():
    from deploy import lambda_client

    chosen = candidates.chosen()
    token = get_parameter(ssm_client(), "jcr/token")
    sbom = publish.download(token, f"{seed.SEED}/{publish.SBOM}").decode()
    results = [line(f"pkg:pypi/{chosen.package}@{chosen.bad}" in sbom, f"the seed's SBOM lists pkg:pypi/{chosen.package}@{chosen.bad}")]
    handler = (ROOT / "demo-states" / seed.SERVICE / seed.VERSION / "handler.py").read_text()
    results.append(line("decompress" not in handler.lower(), "handler.py doesn't contain the word decompress"))
    client = lambda_client(seed.SERVICE)
    with tools.scratch() as work:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", str(work), DECODER], check=True)
        for name, environment in service(seed.SERVICE)["environments"].items():
            url = client.get_function_url_config(FunctionName=environment["function"], Qualifier="live")["FunctionUrl"].rstrip("/")
            status, plain = http_get(f"{url}/api/orders", timeout=15)
            request = urllib.request.Request(f"{url}/api/orders", headers={"Accept-Encoding": "br"})
            with urllib.request.urlopen(request, timeout=15) as response:
                encoding, body = response.headers.get("content-encoding"), response.read()
            results.append(line(status == 200, f"{name}: a plain /api/orders answers {status}"))
            results.append(line(encoding == "br", f"{name}: with Accept-Encoding: br it answers content-encoding {encoding}"))
            results.append(line(encoding == "br" and json.loads(decompressed(body, work)) == json.loads(plain),
                                f"{name}: the body decompresses off the function, to the same orders"))
    return 0 if all(results) else 1


# Check 5

def refused(call, what, **kwargs):
    try:
        call(**kwargs)
    except Exception as error:
        code = search.error_name(error)
        return line("AccessDenied" in code, f"{what}: {code}")
    return line(False, f"{what}: NOT refused")


def check_read_only():
    client = readers.reader_client()
    results = []
    for name, block in readers.searchable().items():
        function = block["function"]
        current = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
        results.append(line(True, f"{name}: the role reads its alias, version {current}"))
        # If the role could do these, the calls would change nothing: the same version, no new code, a broken zip.
        results.append(refused(client.update_alias, f"{name}: UpdateAlias", FunctionName=function, Name="live", FunctionVersion=current))
        results.append(refused(client.update_function_code, f"{name}: UpdateFunctionCode", FunctionName=function, ZipFile=b"PK"))
        results.append(refused(client.publish_version, f"{name}: PublishVersion", FunctionName=function))
    reader = readers.evidence_reader()
    request = urllib.request.Request(f"{reader.base}/artifactory/{seed.SEED}/refused.txt", data=b"refused", method="PUT",
                                     headers={"Authorization": f"Bearer {reader.token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    results.append(line(status in (401, 403), f"the evidence reader's JFrog token: an upload answers {status}"))
    return 0 if all(results) else 1


# Check 6

class Tampered(readers.JFrog):
    """The real JFrog, with one file answered differently, in this run only. Nothing in JFrog changes."""

    def __init__(self, inner, answer):
        super().__init__(inner.token, inner.base)
        self.answer = answer

    def get(self, path):
        if path.endswith(f"/{publish.SBOM}"):
            return self.answer
        return super().get(path)


class Denied:
    """A Lambda client whose every call is refused, as a role without the rights is."""

    def get_alias(self, **kwargs):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "refused"}}, "GetAlias")


def check_rows():
    chosen = candidates.chosen()
    facts = search.read_facts(chosen.cve)
    client, jfrog = readers.reader_client(), readers.evidence_reader()
    other = json.dumps({"bomFormat": "CycloneDX", "components": []}).encode()
    results = []
    for name, block in readers.searchable().items():
        real = search.searched(name, block, client, jfrog, facts)
        print(f"     {real.says}: the row as it stands")
        swapped = search.searched(name, block, client, Tampered(jfrog, (200, other)), facts)
        results.append(line(swapped.kind == search.WARNING and "doesn't match" in swapped.says, f"swapped SBOM: {swapped.says}"))
        missing = search.searched(name, block, client, Tampered(jfrog, (404, b"")), facts)
        results.append(line(missing.kind == search.WARNING and "no SBOM stored" in missing.says, f"no SBOM: {missing.says}"))
        denied = search.searched(name, block, Denied(), jfrog, facts)
        results.append(line(denied.kind == search.WARNING and "couldn't read" in denied.says, f"role denied: {denied.says}"))
    return 0 if all(results) else 1


CHECKS = {"cve": check_cve, "seed": check_seed, "read-only": check_read_only, "rows": check_rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("check", choices=list(CHECKS))
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        return host.hand_over("patch-clock-check", "demos/patch_clock/first_day.py", [args.check], title=f"patch_clock first_day {args.check}")
    sys.stdout.reconfigure(line_buffering=True)
    return CHECKS[args.check]()


if __name__ == "__main__":
    sys.exit(main())
