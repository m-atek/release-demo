"""Put demo 5 back to its start: all six targets on quote-service 1.1.0.

    python scripts/demos/clouds/reset.py              everything below
    python scripts/demos/clouds/reset.py --targets    only the six targets, nothing on main, GitHub or Jira

1. Each target in AWS, Azure and Google Cloud moves all traffic back to its
   newest revision or version running 1.1.0. Every target has one: Terraform
   creates them on 1.1.0, and moving back makes no new image.
2. Each target's /health must report 1.1.0, which also wakes it, so the next
   take starts warm.
3. main gets 1.1.0 again (scripts/stage.py, one commit, pushed), the tags and
   GitHub releases of 1.2.0 and 1.2.1 are removed, and ORD-60 is reopened with
   its fix versions unreleased.

Safe to run twice: a target already on 1.1.0 stays there. In Jenkins it signs
in with the build's ID tokens, like the pipeline; on the Mac with your own
logins (AWS profile, az login, gcloud auth login). Takes under two minutes.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

from check import wait_for_version  # noqa: E402
from common import ROOT, fail, service  # noqa: E402
from deploy import clouds_in_order, sign_in_all  # noqa: E402

START = "1.1.0"
RELEASED = ["1.2.0", "1.2.1"]
JIRA_KEY = "ORD-60"


def reset_targets(config, clouds):
    failed = []
    for environment in config["environments"]:
        targets = sign_in_all(config, clouds, environment)
        for t in targets.values():
            live = t.live()
            if live.version != START:
                revision = t.find(START)
                if revision is None:
                    failed.append(f"{t}: no revision runs {START}")
                    continue
                t.move(revision.id)
                print(f"{t}: back on {revision.id} ({START}), was {live.id} ({live.version})")
            else:
                print(f"{t}: already on {START}")
            ok, detail = wait_for_version(t.url(), START, t.key, 90)
            print(f"{'ok  ' if ok else 'FAIL'} {t}: /health reports {START if ok else detail}")
            if not ok:
                failed.append(f"{t}: /health reports {detail}")
    return failed


def run(*args):
    result = subprocess.run([sys.executable, *args], cwd=ROOT)
    return result.returncode == 0


def reset_records(config):
    name = config["name"]
    ok = run("scripts/stage.py", name, START)
    for version in RELEASED:
        ok &= run("scripts/tag.py", "--delete", f"{name}-v{version}")
        ok &= run("scripts/jira.py", "reopen", JIRA_KEY, f"{name} {version}")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--targets", action="store_true", help="only the six targets")
    args = parser.parse_args()

    started = time.monotonic()
    config = service("quote-service")
    failed = reset_targets(config, clouds_in_order(config))
    if not args.targets and not reset_records(config):
        failed.append("main, GitHub or Jira: see above")
    if failed:
        fail("Not reset:\n  " + "\n  ".join(failed))
    print(f"Demo 5 is back on {START} everywhere, in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
