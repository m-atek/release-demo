"""Put video 26 back to its starting state, for the next take.

    .venv/bin/python scripts/reset.py can-i              from the Mac
    .venv/bin/python scripts/reset.py can-i --dry-run    every step it would take, and nothing else

Video 8's reset without its stage, so both namespaces are on 1.8.2. Then,
through host.py, as the admin: peek gone from both namespaces, cluster/ and
take/before.yaml applied, busybox:1.37 pulled onto the node, so a first pull
never lands on the clock. It stores the mark who.py reads, the time on the
cluster's own clock once all of that is done, then stages catalog 1.9.1 and
ends "Ready for a take". Under two minutes, safe to run twice; a pod left from
a take adds about 30 seconds. Like video 8's reset, it doesn't reopen ORD-90
or un-release "catalog 1.9.1": the tag step says "already released" on a repeat.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import remote  # noqa: E402
import who  # noqa: E402
from common import ROOT, fail  # noqa: E402

VIDEO8_RESET = HERE.parent / "kubernetes" / "reset.py"
SERVICE, VERSION = "catalog", "1.9.1"
STEPS = ["put both namespaces on 1.8.2 (video 8's reset, without its stage)",
         "remove peek from both namespaces and wait until its pods are gone",
         "apply cluster/ and take/before.yaml, and label production's Role",
         "pull busybox:1.37 onto the node",
         "store the mark who.py reads",
         f"stage {SERVICE} {VERSION} on main"]


def mark_in(output):
    """The last line on_host.py prepare prints: mark <UTC time>."""
    for line in reversed(output.splitlines()):
        if line.startswith("mark "):
            return line.split()[1]
    fail(f"The cluster's preparation printed no mark:\n{output}")


def run_script(path, *arguments):
    if subprocess.run([sys.executable, str(path), *arguments]).returncode != 0:
        fail(f"{Path(path).name} {' '.join(arguments)} failed: the take isn't ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the steps, change nothing")
    args = parser.parse_args()
    if args.dry_run:
        for step in STEPS:
            print(f"  would {step}")
        return

    started = time.monotonic()
    run_script(VIDEO8_RESET, "--no-stage")
    code, output = remote.run(f"{remote.ON_HOST} prepare", timeout=300)
    print(output, end="" if output.endswith("\n") else "\n")
    if code != 0:
        fail("The cluster's preparation failed. Is the cluster up? scripts/demo.sh up can-i")
    who.store_mark(mark_in(output))
    run_script(ROOT / "scripts" / "stage.py", SERVICE, VERSION)
    print(f"Reset done in {time.monotonic() - started:.0f}s.")
    print("Ready for a take")


if __name__ == "__main__":
    main()
