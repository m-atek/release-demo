"""Put demo 8 back to its starting state: the cluster on 1.8.2, and 1.9.0 staged on main.

    python scripts/demos/kubernetes/reset.py              from the Mac: the cluster through SSM, then stage 1.9.0
    python scripts/demos/kubernetes/reset.py --no-stage   the cluster only
    python scripts/demos/kubernetes/reset.py --here       on the tools host: what the Mac's run calls there

The 1.8.2 package comes from demos-releases/catalog/1.8.2 in JCR. The first time,
when it isn't there yet, it is built from demo-states/catalog/1.8.2 and stored
there, as Terraform's first apply does for orders-service, so every reset
deploys the same image digest. A namespace already on 1.8.2 with every pod
ready is left alone, so a second run changes nothing; any other gets 1.8.2
through the same Helm deploy as a release. Both namespaces at once: well
under 2 minutes. Run scripts/demo.sh up kubernetes first.

What every service's reset does (Jira versions, tags, open changes, release
copies) is scripts/reset.py's. This does what is particular to the cluster.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# This folder first: its build.py, deploy.py and check.py share names with the Lambda ones in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[2]))
import kube  # noqa: E402
from common import ROOT, fail, get_parameter, git, service, ssm_client  # noqa: E402

SERVICE = "catalog"
BASELINE = "1.8.2"
TAKE = "1.9.0"


def baseline_package(token, commit):
    """The 1.8.2 package: from catalog-releases, or built and stored there the first time."""
    import build
    import publish

    root = publish.releases_root(SERVICE)
    path = f"{root}/{BASELINE}/{SERVICE}-{BASELINE}.zip"
    status, body = publish.request("GET", path, token)
    if status == 200:
        print(f"1.8.2 from JCR: {path}")
        return body
    with tempfile.TemporaryDirectory() as out:
        info = build.build(SERVICE, version_dir=ROOT / "demo-states" / SERVICE / BASELINE, out=out, commit=commit)
        package = (Path(out) / SERVICE / info["package"]).read_bytes()
        headers = {"X-Checksum-Sha256": hashlib.sha256(package).hexdigest(), "Content-Type": "application/zip"}
        # JCR drops the connection on an upload to a repository it doesn't have, so ask first.
        status = publish.request("GET", f"api/storage/{root.split('/')[0]}", token)[0]
        if status == 200:
            status, _ = publish.request("PUT", path, token, package, headers)
        if status in (200, 201):
            publish.request("PUT", f"{root}/{BASELINE}/build-info.json", token,
                            (Path(out) / SERVICE / "build-info.json").read_bytes(),
                            {"Content-Type": "application/json"})
            print(f"1.8.2 built from demo-states/{SERVICE}/{BASELINE} and stored as {path}")
        else:
            print(f"1.8.2 built from demo-states/{SERVICE}/{BASELINE}. Not stored: JCR answered {status} "
                  f"for {path}. Create the repository {root.split('/')[0]} (scripts/jcr_setup.py lists the steps).")
        return package


def on_baseline(config, environment):
    """True when the namespace runs 1.8.2 with every desired pod ready and nothing pending."""
    env = config["environments"][environment]
    name, namespace = config["kubernetes"]["release"], env["namespace"]
    with kube.credentials(quiet=True) as kubeconfig:
        revisions = kube.history(kubeconfig, name, namespace)
        if revisions and revisions[-1]["status"].startswith("pending"):
            last = kube.deployed(revisions)
            if not last:
                fail(f"{namespace}: Helm's first install is stuck in {revisions[-1]['status']}. "
                     f"Remove it with helm uninstall {name} -n {namespace}, then reset again.")
            print(f"{namespace}: release stuck in {revisions[-1]['status']}, rolling back to revision {last['revision']}")
            kube.helm(kubeconfig, "rollback", name, str(last["revision"]), "--namespace", namespace,
                      "--wait=watcher", "--timeout", "3m")
            revisions = kube.history(kubeconfig, name, namespace)
        current = kube.deployed(revisions)
        api = kube.Api(kubeconfig)
        pods = [kube.pod_summary(p) for p in api.pods(namespace, name)]
    ready = [p for p in pods if p[0] == BASELINE and p[1] and not p[2]]
    if current and current["app_version"] == BASELINE and len(ready) == env["replicas"] == len(pods):
        print(f"{namespace}: already on {BASELINE}, revision {current['revision']}, {len(ready)} pods ready")
        return True
    print(f"{namespace}: on {current['app_version'] if current else 'nothing'}, going to {BASELINE}")
    return False


def reset_environment(environment, commit):
    """One namespace, in its own process: deploy keeps a run state per process."""
    import deploy
    import run_state

    config = service(SERVICE)
    if on_baseline(config, environment):
        return 0
    token = get_parameter(ssm_client(), "jcr/token")
    package = baseline_package(token, commit)
    state = {"service": SERVICE, "version": BASELINE}
    run_state.save(state)
    return deploy.deploy(config, environment, package, state, token)


def here(commit):
    """Both namespaces at once, each line prefixed with its namespace."""
    started = time.monotonic()
    config = service(SERVICE)
    children = {}
    for environment in config["environments"]:
        workspace = tempfile.mkdtemp(prefix=f"reset-{environment}-")
        children[environment] = subprocess.Popen(
            [sys.executable, __file__, "--here", "--only", environment, "--commit", commit],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env={**os.environ, "WORKSPACE": workspace})

    def relay(environment, stream):
        for line in stream:
            print(f"[{environment}] {line}", end="", flush=True)

    relays = [threading.Thread(target=relay, args=(e, p.stdout)) for e, p in children.items()]
    for thread in relays:
        thread.start()
    codes = {e: p.wait() for e, p in children.items()}
    for thread in relays:
        thread.join()
    took = kube.clock(time.monotonic() - started)
    if any(codes.values()):
        fail(f"Reset failed after {took}: {', '.join(e for e, c in codes.items() if c)}")
    print(f"Cluster reset in {took}: staging and production on {BASELINE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--here", action="store_true", help="run on the tools host")
    parser.add_argument("--only", help=argparse.SUPPRESS)
    parser.add_argument("--commit", default="", help=argparse.SUPPRESS)
    parser.add_argument("--no-stage", action="store_true", help=f"don't stage {TAKE} on main")
    args = parser.parse_args()

    if args.here and args.only:
        sys.exit(reset_environment(args.only, args.commit))
    if args.here:
        here(args.commit)
        return

    import host

    started = time.monotonic()
    commit = git("log", "-1", "--format=%H", "--", f"demo-states/{SERVICE}/{BASELINE}", check=False).stdout.strip()
    code, output = host.run(f"python3 scripts/demos/kubernetes/reset.py --here --commit {commit or 'baseline'}",
                            timeout=600)
    print(output, end="")
    if code != 0:
        fail("The cluster reset failed. Is the cluster up? scripts/demo.sh status kubernetes")
    if not args.no_stage:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "stage.py"), SERVICE, TAKE], check=True)
    print(f"Reset done in {kube.clock(time.monotonic() - started)}.")


if __name__ == "__main__":
    main()
