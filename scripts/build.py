"""Stage 1, Build: package a service as a Lambda zip.

    python scripts/build.py orders-service [--jira-key ORD-12]

Writes dist/<service>/<service>-<version>.zip and build-info.json with the
commit, version, Jira key, build link and SHA-256. The zip is reproducible:
the same source gives the same checksum. Tests stay out of the package.
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import ci_env
from common import ROOT, fail, git, service, version_of, write_json

EXCLUDE_DIRS = {"tests", "__pycache__", ".pytest_cache"}
EXCLUDE_FILES = {"requirements.txt"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def current_commit():
    """The CI's commit (Jenkins' GIT_COMMIT, scripts/ci_env.py). Locally, HEAD,
    marked when the tree has changes."""
    if ci_env.current().commit:
        return ci_env.current().commit
    head = git("rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        return "uncommitted"
    dirty = git("status", "--porcelain", check=False).stdout.strip()
    return head.stdout.strip() + ("-dirty" if dirty else "")


def install_requirements(requirements, target, runtime):
    """Vendor dependencies as Lambda's Linux runtime needs them, wheels only."""
    python_version = runtime.removeprefix("python")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-compile",
         "--requirement", str(requirements), "--target", str(target),
         "--platform", "manylinux2014_x86_64", "--implementation", "cp",
         "--python-version", python_version, "--only-binary=:all:"],
        check=True,
    )


def package_files(directory):
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if path.is_file() and not EXCLUDE_DIRS & set(relative.parts) and relative.name not in EXCLUDE_FILES:
            yield path, relative


def write_zip(source, destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, relative in package_files(source):
            info = zipfile.ZipInfo(relative.as_posix(), ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    config = service(args.service)
    source = config["dir"]
    if not (source / "VERSION").exists():
        fail(f"{config['path']}/VERSION is missing. Stage a version first: scripts/stage.py {args.service} 1.3.0")
    version = version_of(source)
    commit = current_commit()

    out = args.out / args.service
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"{args.service}-{version}.zip"

    with tempfile.TemporaryDirectory() as temp:
        build_dir = Path(temp) / "package"
        shutil.copytree(source, build_dir, ignore=shutil.ignore_patterns(*EXCLUDE_DIRS))
        if (source / "requirements.txt").exists():
            install_requirements(source / "requirements.txt", build_dir, config["runtime"])
        write_json(build_dir / "build_info.json", {"service": args.service, "version": version, "commit": commit})
        write_zip(build_dir, package)

    info = {
        "service": args.service,
        "version": version,
        "commit": commit,
        "jira_key": args.jira_key,
        "build_url": ci_env.current().run_url or None,
        "package": package.name,
        "sha256": sha256(package),
        "size_bytes": package.stat().st_size,
    }
    write_json(out / "build-info.json", info)
    print(f"Built {package.relative_to(ROOT)}")
    print(f"sha256 {info['sha256']}")


if __name__ == "__main__":
    main()
