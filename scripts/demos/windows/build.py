"""Stage 1, Build: compile store-sync, the .NET Framework 4.8 service, into a zip.

    python scripts/demos/windows/build.py store-sync [--jira-key ORD-31]
    python scripts/demos/windows/build.py store-sync --source demo-states/store-sync/3.2.0 --out /tmp/seed

Runs in the .NET toolchain container (jenkins/toolchain/dotnet.Dockerfile):
the .NET 10 SDK compiles the SDK-style net48 project on Linux against
Microsoft's reference assemblies. Writes dist/<service>/<service>-<version>.zip
with StoreSync.exe, its config and StoreSync.Core.dll, and build-info.json with
the SHA-256. The build is deterministic and the zip has fixed timestamps, so
the same source gives the same checksum.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from build import current_commit, sha256, write_zip  # noqa: E402
from common import ROOT, fail, service, version_of, write_json  # noqa: E402

PROJECT = "src/StoreSync/StoreSync.csproj"
# What the service needs on a server, and nothing else.
PACKAGE_FILES = ["StoreSync.exe", "StoreSync.exe.config", "StoreSync.Core.dll"]


def compile_service(source, work):
    """dotnet publish from a copy of the source, so no bin/ or obj/ lands in the repository."""
    tree = Path(work) / "src"
    shutil.copytree(source, tree, ignore=shutil.ignore_patterns("bin", "obj", "tests", ".release"))
    out = Path(work) / "publish"
    result = subprocess.run(
        ["dotnet", "publish", str(tree / PROJECT), "--configuration", "Release", "--output", str(out),
         "-p:ContinuousIntegrationBuild=true", "--nologo", "--verbosity", "quiet"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-4000:], result.stderr[-2000:], sep="\n", file=sys.stderr)
        fail("dotnet publish failed")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    parser.add_argument("--source", type=Path, default=None, help="build this folder instead of the service's")
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("WORKSPACE", ROOT)) / "dist")
    args = parser.parse_args()

    config = service(args.service)
    source = (args.source if args.source and args.source.is_absolute() else ROOT / args.source) if args.source else config["dir"]
    if not (source / "VERSION").exists():
        fail(f"{source}/VERSION is missing.")
    if not shutil.which("dotnet"):
        fail("No dotnet here. Build in the toolchain container: jenkins/toolchain/dotnet.Dockerfile")
    version = version_of(source)

    out = args.out / args.service
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"{args.service}-{version}.zip"

    with tempfile.TemporaryDirectory() as work:
        published = compile_service(source, work)
        staging = Path(work) / "package"
        staging.mkdir()
        for name in PACKAGE_FILES:
            if not (published / name).exists():
                fail(f"dotnet publish didn't produce {name}")
            shutil.copy2(published / name, staging / name)
        shutil.copy2(source / "VERSION", staging / "VERSION")
        write_zip(staging, package)

    info = {
        "service": args.service,
        "version": version,
        "commit": current_commit(),
        "jira_key": args.jira_key,
        "build_url": os.environ.get("BUILD_URL"),
        "package": package.name,
        "sha256": sha256(package),
        "size_bytes": package.stat().st_size,
    }
    write_json(out / "build-info.json", info)
    print(f"Built {package}")
    print(f"sha256 {info['sha256']}")


if __name__ == "__main__":
    main()
