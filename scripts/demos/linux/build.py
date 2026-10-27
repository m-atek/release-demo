"""Stage 1, Build: package inventory, a Java web app, as a WAR in a zip.

    python scripts/demos/linux/build.py inventory --jira-key ORD-44
    python scripts/demos/linux/build.py inventory --source demo-states/inventory/2.7.3 --out dist/baseline

Runs mvn package (the tests are stage 2's) in jenkins/toolchain/java.Dockerfile
and checks that the pom's version is the one in VERSION. Writes
dist/<service>/<service>-<version>.zip with inventory.war, VERSION and
build_info.json, plus build-info.json with the commit and the SHA-256, like
scripts/build.py. The WAR has fixed entry times and the zip fixed dates, so
the same source gives the same checksum.

--source builds a demo state instead of the service folder, in a copy, for
the starting release that scripts/demos/linux/up.sh and reset.py install.
"""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from build import current_commit, sha256, write_zip  # noqa: E402
from common import ROOT, fail, service, version_of, write_json  # noqa: E402
from maven import mvn, pom_version  # noqa: E402


def package(name, source, out, jira_key=None, commit=None):
    """Build source and write the zip and build-info.json to out/<name>. Returns build-info."""
    version = version_of(source)
    if pom_version(source) != version:
        fail(f"{source}/pom.xml says {pom_version(source)}, VERSION says {version}: make them the same.")
    if mvn(source, "-DskipTests", "package") != 0:
        fail("mvn package failed")
    war = Path(source) / "target" / "inventory.war"
    if not war.exists():
        fail(f"mvn package made no {war}")

    commit = commit or current_commit()
    target = Path(out) / name
    target.mkdir(parents=True, exist_ok=True)
    zip_path = target / f"{name}-{version}.zip"
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp)
        shutil.copy2(war, staging / "inventory.war")
        (staging / "VERSION").write_text(version + "\n")
        write_json(staging / "build_info.json", {"service": name, "version": version, "commit": commit})
        write_zip(staging, zip_path)

    info = {
        "service": name,
        "version": version,
        "commit": commit,
        "jira_key": jira_key,
        "build_url": os.environ.get("BUILD_URL"),
        "package": zip_path.name,
        "sha256": sha256(zip_path),
        "size_bytes": zip_path.stat().st_size,
    }
    write_json(target / "build-info.json", info)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--source", type=Path, help="a demo state to build instead of the service folder")
    args = parser.parse_args()

    config = service(args.service)
    if args.source:
        source = args.source if args.source.is_absolute() else ROOT / args.source
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "source"
            shutil.copytree(source, copy, ignore=shutil.ignore_patterns("target"))
            info = package(args.service, copy, args.out, args.jira_key, commit=f"demo-state {source.name}")
    else:
        if not (config["dir"] / "VERSION").exists():
            fail(f"{config['path']}/VERSION is missing. Stage a version first: scripts/stage.py {args.service} 2.7.3")
        info = package(args.service, config["dir"], args.out, args.jira_key)
    print(f"Built {Path(args.out) / args.service / info['package']}")
    print(f"sha256 {info['sha256']}")


if __name__ == "__main__":
    main()
