"""After a take of video 9: is production where it was, and the take's workspace gone.

    python scripts/reset.py agent-access              check and tidy
    python scripts/reset.py agent-access --dry-run    say what it would do

agent.sh records in each workspace's take.json where orders-service-
production's live alias pointed before Claude Code started. This checks
that it still points there, then removes the workspace (a folder under the
temporary directory) and a take's container if one is left. It changes
nothing in AWS: if production moved, it says so and stops, and you look.
Safe to run twice.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_access as agent  # noqa: E402


def workspaces(base=None):
    return sorted(Path(base or tempfile.gettempdir()).glob(f"{agent.WORKSPACE_PREFIX}*"))


def reset(dry_run=False, base=None, live=None):
    """0 when production is where every take found it, 1 when it moved."""
    found = workspaces(base)
    if not found:
        print("No take workspaces left: nothing to tidy.")
    current = live or agent.production_live()
    moved = False
    for folder in found:
        record = folder / "take.json"
        before = json.loads(record.read_text())["production"] if record.exists() else None
        if before is None:
            print(f"  {folder.name}: no take.json (agent.sh stopped before Claude Code started)")
        elif before["version"] == current["version"]:
            print(f"  {folder.name}: {current['function']} live on version {current['version']} "
                  f"({current['app_version']}), as before the take")
        else:
            moved = True
            print(f"  {folder.name}: {current['function']} live moved from version {before['version']} "
                  f"({before['app_version']}) to {current['version']} ({current['app_version']}). Look at it now.")
            continue  # keep the evidence
        if dry_run:
            print(f"  would remove {folder}")
        else:
            shutil.rmtree(folder)
            print(f"  removed {folder}")
    leftover = subprocess.run(["docker", "ps", "-q", "--filter", "name=^release-demo-coding-agent-"],
                              capture_output=True, text=True).stdout.split()
    for container in leftover:
        if dry_run:
            print(f"  would stop the take's container {container}")
        else:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            print(f"  stopped the take's container {container}")
    print("Production moved during a take: see above." if moved else "Ready for the next take: run agent.sh.")
    return 1 if moved else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return reset(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
