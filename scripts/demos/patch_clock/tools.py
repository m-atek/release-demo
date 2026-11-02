"""Syft and Grype as the release pipeline runs them: the images toolchain.groovy pins, in Docker
on the tools host, with the shared caches on the data volume.

The pins are read from toolchain.groovy with the regex scripts/tests/test_github_actions.py uses,
so they can't drift. Each container runs as the owner of its cache, the Jenkins agent's user,
even when a reset handed over from the Mac runs as root: a root-owned file in the cache would
stop the next release's scan.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TOOLCHAIN = ROOT / "jenkins" / "shared-library" / "vars" / "toolchain.groovy"
CACHE = Path(os.environ.get("PATCH_CLOCK_CACHE", "/data/jenkins-agent/cache"))
SYFT_SECONDS = 300
GRYPE_SECONDS = 240
UPDATE_SECONDS = 280
CHECK_OFF = {"GRYPE_CHECK_FOR_APP_UPDATE": "false", "SYFT_CHECK_FOR_APP_UPDATE": "false"}


class GrypeFailed(Exception):
    """Grype didn't answer, or answered something else than what was asked. Its message is for the log."""


def normalized(name):
    """A package's name as Python's packaging compares it, and as Syft writes it: Brotli is brotli."""
    return re.sub(r"[-_.]+", "-", name or "").lower()


def pins():
    """{"SYFT": image, "GRYPE": image} from toolchain.groovy."""
    found = dict(re.findall(r"@Field String (SONAR|SYFT|GRYPE) = '([^']+)'", TOOLCHAIN.read_text()))
    return {name: found[name] for name in ("SYFT", "GRYPE")}


def cache(name):
    """A cache folder, the Jenkins agent's even when it is made here as root."""
    folder = CACHE / name
    folder.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        owner = CACHE.parent.stat()
        for path in (CACHE, folder):
            os.chown(path, owner.st_uid, owner.st_gid)
    return folder


@contextmanager
def scratch():
    """A folder containers can read and write, gone afterwards."""
    with tempfile.TemporaryDirectory(prefix="patch-clock-", dir=cache("work")) as name:
        folder = Path(name)
        if os.geteuid() == 0:
            owner = CACHE.parent.stat()
            os.chown(folder, owner.st_uid, owner.st_gid)
        yield folder


def container(image, args, cache_folder, mounts=(), env=None, timeout=GRYPE_SECONDS):
    """docker run as the cache's owner. The caller gets the finished process, never an exception for a non-zero exit."""
    owner = cache_folder.stat()
    command = ["docker", "run", "--rm", "--user", f"{owner.st_uid}:{owner.st_gid}", "-v", f"{cache_folder}:/cache"]
    for source, target in mounts:
        command += ["-v", f"{source}:{target}"]
    for name, value in (env or {}).items():
        command += ["-e", f"{name}={value}"]
    return subprocess.run([*command, image, *args], capture_output=True, text=True, timeout=timeout)


# Syft

def syft(package_dir, out_dir, service_name):
    """A CycloneDX SBOM of an unpacked package, as sbomAndScan makes it: dir:, or oci-dir: for a package
    that holds a container image (demos 5 and 8). Returns the SBOM's bytes. package_dir lies inside out_dir."""
    relative = Path(package_dir).relative_to(out_dir).as_posix()
    source = f"oci-dir:/work/{relative}/image" if (Path(package_dir) / "image" / "index.json").exists() else f"dir:/work/{relative}"
    done = container(pins()["SYFT"], [source, "--source-name", service_name, "-o", "cyclonedx-json=/work/sbom.cdx.json"],
                     cache("syft"), mounts=[(out_dir, "/work")], timeout=SYFT_SECONDS,
                     env={"SYFT_CACHE_DIR": "/cache", "TMPDIR": "/cache", **CHECK_OFF})
    if done.returncode != 0:
        print(done.stderr[-2000:], file=sys.stderr)
        raise RuntimeError(f"Syft exited {done.returncode}")
    return (Path(out_dir) / "sbom.cdx.json").read_bytes()


def unpack(package, folder):
    """A package's bytes as a zip, unpacked into folder/package, as the stage unpacks it. Returns that folder."""
    target = Path(folder) / "package"
    (Path(folder) / "package.zip").write_bytes(package)
    with zipfile.ZipFile(Path(folder) / "package.zip") as archive:
        archive.extractall(target)
    return target


# Grype

def grype(args, mounts=(), auto_update=True, timeout=GRYPE_SECONDS):
    env = {"GRYPE_DB_CACHE_DIR": "/cache", "TMPDIR": "/cache", **CHECK_OFF}
    if not auto_update:
        env["GRYPE_DB_AUTO_UPDATE"] = "false"
    return container(pins()["GRYPE"], args, cache("grype"), mounts=mounts, env=env, timeout=timeout)


def json_of(done, what):
    try:
        return json.loads(done.stdout)
    except ValueError:
        print(done.stderr[-2000:], file=sys.stderr)
        raise GrypeFailed(f"{what} exited {done.returncode} without JSON") from None


def db_status():
    """`grype db status -o json`: schemaVersion, built, path, valid and error. It exits non-zero when not valid."""
    return json_of(grype(["db", "status", "-o", "json"], auto_update=False, timeout=60), "grype db status")


def db_update():
    """`grype db update`, whatever the time since the last check. Returns its output for the log."""
    done = grype(["db", "update"], timeout=UPDATE_SECONDS)
    if done.returncode != 0:
        print(done.stderr[-2000:], file=sys.stderr)
        raise GrypeFailed(f"grype db update exited {done.returncode}")
    return done.stdout.strip()


def usable_database():
    """The database's status, updated first when it is missing or older than Grype allows.
    A warm one stays as it is, so a search on it downloads nothing."""
    status = db_status()
    if status.get("valid"):
        return status
    print(f"Grype's database isn't usable ({status.get('error') or 'no reason given'}): updating it")
    db_update()
    status = db_status()
    if not status.get("valid"):
        raise GrypeFailed(f"Grype's database isn't usable after an update: {status.get('error')}")
    return status


def db_file(status):
    """Where the database file lies here: `db status` names it as the container sees it."""
    return cache("grype") / Path(status["path"]).relative_to("/cache")


def db_search(*vulnerabilities):
    """`grype db search --vuln <id> ... -o json`: each record with the packages it affects. Looks by name only."""
    flags = [word for vulnerability in vulnerabilities for word in ("--vuln", vulnerability)]
    return json_of(grype(["db", "search", *flags, "-o", "json"], auto_update=False), "grype db search")


def db_records(vulnerability):
    """`grype db search vuln <id> -o json`: the records themselves, also those that affect no package."""
    return json_of(grype(["db", "search", "vuln", vulnerability, "-o", "json"], auto_update=False), "grype db search vuln")


def scan(sbom_file):
    """`grype sbom:<file> --by-cve -o json` on a stored SBOM, without touching the database, so three can run at once."""
    folder = Path(sbom_file).parent
    done = grype([f"sbom:/work/{Path(sbom_file).name}", "--by-cve", "-o", "json"], mounts=[(folder, "/work:ro")],
                 auto_update=False)
    if done.returncode != 0:
        print(done.stderr[-2000:], file=sys.stderr)
        raise GrypeFailed(f"grype exited {done.returncode}")
    return json_of(done, "grype")
