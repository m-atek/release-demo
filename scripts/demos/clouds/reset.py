"""Put demo 5 back to its start: all six targets on quote-service 1.1.0.

    python scripts/reset.py clouds              or the Jenkins job reset with DEMO=clouds
    python scripts/reset.py clouds --dry-run    every action it would take, and nothing else
    python scripts/demos/clouds/reset.py --targets-only

1. Each target in AWS, Azure and Google Cloud moves all traffic back to its
   newest revision or version running 1.1.0, and its /health must say so,
   which also wakes it for the next take. Every target has one: Terraform
   creates them on 1.1.0, and moving back makes no new image.
2. Like the main demo's reset, it keeps the past and frees only what a take
   creates again, with the main reset's own steps: it cancels quote-service's
   open changes, archives the tags and GitHub releases after 1.1.0 under
   takes/, archives JFrog's release copies after 1.1.0 (on the tools host,
   where JFrog answers), un-releases the Jira versions and reopens ORD-60, and
   stages 1.1.0 on main.

Each step looks before it acts, so a second run finds nothing to do. In
Jenkins it reaches the clouds with the build's ID tokens, like the pipeline;
on the Mac with your own logins (AWS profile, az login, gcloud auth login).
Under two minutes.
"""

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

from check import wait_for_version  # noqa: E402
from common import ROOT, service  # noqa: E402
from deploy import clouds_in_order, sign_in_all  # noqa: E402

SERVICE = "quote-service"
START = "1.1.0"
ISSUES = ["ORD-60"]


def main_reset():
    """scripts/reset.py, with its steps pointed at quote-service and 1.1.0."""
    spec = importlib.util.spec_from_file_location("shared_reset", ROOT / "scripts" / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    shared.SERVICE, shared.BASELINE, shared.DEMO_ISSUES = SERVICE, START, ISSUES
    return shared


def targets(reset, config):
    for environment in config["environments"]:
        for t in sign_in_all(config, clouds_in_order(config), environment).values():
            live = t.live()
            if live.version == START:
                print(f"  {t}: already on {START}")
            else:
                revision = t.find(START)
                if revision is None:
                    raise RuntimeError(f"{t} has no revision running {START}")
                reset.act(f"move {t} to {revision.id} ({START}), from {live.id} ({live.version})",
                          lambda t=t, revision=revision: t.move(revision.id))
            if not reset.dry_run:
                ok, detail = wait_for_version(t.url(), START, t.key, 90)
                print(f"  {'ok  ' if ok else 'FAIL'} {t}: /health reports {START if ok else detail}")
                if not ok:
                    raise RuntimeError(f"{t} doesn't answer {START}")


def jfrog_reachable():
    from common import http_get

    return http_get(os.environ.get("JCR_URL", "http://localhost:8082") + "/artifactory/api/system/ping", timeout=3)[0] == 200


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--targets-only", action="store_true", help="only the six targets")
    args = parser.parse_args()

    started = time.monotonic()
    config = service(SERVICE)
    shared = main_reset()
    reset = shared.Reset(args.dry_run)
    print(f"Reset demo 5 to {SERVICE} {START} in every cloud, keeping the past"
          f"{' (dry run: nothing changes)' if args.dry_run else ''}.")
    reset.step(f"Targets: all six on {START}", lambda: targets(reset, config))
    if not args.targets_only:
        reset.step(f"ServiceNow: open {SERVICE} changes", lambda: shared.cancel_changes(reset))
        reset.step(f"GitHub: tags and releases after {START}", lambda: shared.github_takes(reset))
        if jfrog_reachable():
            reset.step(f"JFrog: release copies after {START}", lambda: shared.jfrog_takes(reset))
        else:
            print("JFrog: not reachable from here, so its release copies stay. "
                  "Run the Jenkins job reset with DEMO=clouds to archive them too.")
        reset.step(f"Jira: {SERVICE} versions and {', '.join(ISSUES)}", lambda: shared.jira_versions(reset))
        reset.step(f"main: {SERVICE} {START}", lambda: shared.stage_baseline(reset))
    if reset.failed:
        print(f"Not everything reset: {', '.join(reset.failed)}. The messages above say why.")
        return 1
    print(f"Demo 5 is back on {START}{' (dry run)' if args.dry_run else ''}, in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
