"""Stage 11, Check production: does every inventory server run the version?

    python scripts/demos/linux/check.py inventory production --version 2.8.0 --url ""

Each server is checked on its own /health, from inside: there is no load
balancer and nothing listens on the network. The cloud servers through
Systems Manager from here, the data-center servers from dc-agent. --url is
accepted for the pipeline's sake and not used. Prints one line per server,
ok or FAIL, and exits 1 if any server doesn't report the version.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import fleet  # noqa: E402
from common import fail  # noqa: E402


def check(version, names=None, secrets=None):
    """[(server, ok, what it said)] for every server, in rollout order."""
    names = names or [s["name"] for s in fleet.servers()]
    results = []
    for site, members in fleet.by_site(names).items():
        site_names = [s["name"] for s in members]
        extra = {"action": "check", "version": version, "rollout_serial": len(site_names), "rollout_max_fail": 100}
        if site == "cloud":
            results += fleet.run_cloud("release.yml", extra, site_names, prefix="  cloud | ")[1]
        else:
            results += fleet.run_dc("release.yml", extra, site_names, secrets=secrets, prefix="  data center | ")[1]
    found = {r["server"]: r for r in results}
    lines = []
    for name in names:
        result = found.get(name)
        if result is None:
            lines.append((name, False, "didn't answer the check"))
        else:
            lines.append((name, result["result"] == "ok", result["detail"].removeprefix("ok   ").removeprefix("FAIL ")))
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", default="", help="unused: each server is checked from inside")
    args = parser.parse_args()
    if args.service != fleet.SERVICE or args.environment != "production":
        fail(f"This check is for {fleet.SERVICE} in production")

    lines = check(args.version)
    for name, ok, what in lines:
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {what}")
    if not all(ok for _, ok, _ in lines):
        print("Production check failed")
        sys.exit(1)
    print("Production check passed")


if __name__ == "__main__":
    main()
