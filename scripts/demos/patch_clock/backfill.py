"""An SBOM for a live service whose release folder has none: it released before SBOMs travelled with packages.

    python scripts/demos/patch_clock/backfill.py                      every service with live: in services.yaml
    python scripts/demos/patch_clock/backfill.py pricing-api --dry-run

Reads what is live, finds the release copy that is that package, downloads it with release-pipeline's
token (which may deploy and not delete), checks its SHA-256, runs the pinned Syft over it as the
pipeline's SBOM and scan stage does (dir:, or oci-dir: for a package that holds an image), and stores
the SBOM next to the package with sbom-info.json. A folder that has one is left alone. Runs on the tools
host, where JFrog and Docker are; reset.py patch-clock calls it for pricing-api and quote-service.
"""

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import live as readers  # noqa: E402
import publish  # noqa: E402
import tools  # noqa: E402
from common import fail, get_parameter, ssm_client  # noqa: E402


def sbom_of_package(token, folder, info, service_name):
    """The SBOM of the stored package, after checking the package's SHA-256."""
    package = publish.download(token, f"{folder}/{info['package']}")
    actual = hashlib.sha256(package).hexdigest()
    if actual != info["sha256"]:
        fail(f"{folder}/{info['package']} has SHA-256 {actual}, but its build-info.json says {info['sha256']}: no SBOM for it.")
    with tools.scratch() as work:
        return tools.syft(tools.unpack(package, work), work, service_name)


def backfill(names, act, client=None, token=None):
    """Each named service's SBOM, through act(what, do) as Reset.act takes it. Returns how many have one now."""
    client = client or readers.reader_client()
    token = token or get_parameter(ssm_client(), "jcr/token")
    jfrog = readers.JFrog(token)
    have = 0
    blocks = readers.searchable()
    for name in names:
        what = readers.read_live(client, blocks[name])
        folder, info = readers.release_folder(jfrog, name, what)
        if folder is None:
            print(f"  {name} {what.version}: no release copy of what is live, so nothing to read an SBOM from")
            continue
        if readers.present(jfrog, f"{folder}/{publish.SBOM}") is not None:
            print(f"  {name} {what.version}: {folder} has its SBOM")
            have += 1
            continue

        def store(folder=folder, info=info, name=name):
            publish.upload_sbom(token, folder, info, sbom_of_package(token, folder, info, name))

        act(f"make the SBOM of {name} {what.version} and store it in {folder}", store)
        have += 1
    return have


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", nargs="*", help="default: every service with live: in services.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print what it would make, change nothing")
    args = parser.parse_args()

    def act(what, do):
        if args.dry_run:
            print(f"  would {what}")
            return
        do()
        print(f"  {what}: done")

    backfill(args.service or list(readers.searchable()), act)
    return 0


if __name__ == "__main__":
    sys.exit(main())
