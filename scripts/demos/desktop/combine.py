"""Stage 1, Build, on the tools host: one package from what the build agents made.

    python scripts/demos/desktop/combine.py store-assistant --jira-key ORD-71

The Mac agent leaves dist/<service>/parts/mac/ (the notarized .pkg and
mac-build.json), the Windows agent dist/<service>/parts/windows/ (the signed
.msi and windows-build.json); the pipeline stashes both and unstashes them
here. This checks each file against the checksum its agent recorded and that
every part is the version in VERSION, then writes the package the rest of the
pipeline handles like any other: dist/<service>/<service>-<version>.zip with
both installers and their records, and build-info.json with its SHA-256.

The tracks come from services.yaml (desktop.tracks), so the Windows track can
be switched off without touching the pipeline.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from build import current_commit, sha256, write_zip  # noqa: E402  (scripts/build.py)
from common import ROOT, fail, service, version_of, write_json  # noqa: E402

PARTS = {
    "mac": {"record": "mac-build.json", "file": "pkg", "extras": ["notarization-log.json"]},
    "windows": {"record": "windows-build.json", "file": "msi", "extras": []},
}


def check_part(directory, track, version):
    """The part's record, after checking its installer against the recorded SHA-256."""
    spec = PARTS[track]
    record_path = directory / spec["record"]
    if not record_path.exists():
        fail(f"No {spec['record']} in {directory.relative_to(ROOT)}: the {track} agent's build didn't arrive.")
    record = json.loads(record_path.read_text())
    installer = record[spec["file"]]
    path = directory / installer["name"]
    if not path.exists():
        fail(f"{installer['name']} is missing from {directory.relative_to(ROOT)}.")
    if record["version"] != version:
        fail(f"The {track} agent built {record['version']}, but VERSION says {version}.")
    if sha256(path) != installer["sha256"]:
        fail(f"{installer['name']} doesn't match the SHA-256 the {track} agent recorded.")
    print(f"ok   {track}: {installer['name']}, SHA-256 {installer['sha256'][:16]}..., as its agent recorded")
    return record, path


def summary(track, record):
    if track == "mac":
        return {"signed_by": record["app_signature"]["authority"], "notarization": record["notarization"],
                "gatekeeper": record["gatekeeper"], "standin": record["standin"]}
    return {"signed_by": record["signature"]["signer"], "signature_status": record["signature"]["status"],
            "test_certificate": record["signature"]["test_certificate"], "standin": record["standin"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    args = parser.parse_args()

    config = service(args.service)
    version = version_of(config["dir"])
    out = ROOT / "dist" / args.service
    tracks = config.get("desktop", {}).get("tracks", ["mac", "windows"])
    artifacts, records = {}, {}

    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp) / "package"
        staging.mkdir()
        (staging / "VERSION").write_text(version + "\n")
        for track in tracks:
            directory = out / "parts" / track
            record, path = check_part(directory, track, version)
            records[track] = record
            (staging / track).mkdir()
            shutil.copy2(path, staging / track / path.name)
            for name in [PARTS[track]["record"], *PARTS[track]["extras"]]:
                if (directory / name).exists():
                    shutil.copy2(directory / name, staging / track / name)
            installer = record[PARTS[track]["file"]]
            artifacts[PARTS[track]["file"]] = {"name": installer["name"], "path": f"{track}/{installer['name']}",
                                               "sha256": installer["sha256"], "sha512": installer.get("sha512"),
                                               **summary(track, record)}
        commit = current_commit()
        write_json(staging / "build_info.json", {"service": args.service, "version": version, "commit": commit})
        package = out / f"{args.service}-{version}.zip"
        write_zip(staging, package)

    info = {
        "service": args.service,
        "version": version,
        "commit": commit,
        "jira_key": args.jira_key,
        "build_url": os.environ.get("BUILD_URL"),
        "package": package.name,
        "sha256": sha256(package),
        "size_bytes": package.stat().st_size,
        "artifacts": artifacts,
    }
    write_json(out / "build-info.json", info)
    print(f"Built {package.relative_to(ROOT)} with {', '.join(a['name'] for a in artifacts.values())}")
    print(f"sha256 {info['sha256']}")


if __name__ == "__main__":
    main()
