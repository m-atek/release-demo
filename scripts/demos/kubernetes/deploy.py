"""Stages 6 and 10 for Kubernetes: deploy the exact stored package with Helm.

    python scripts/demos/kubernetes/deploy.py catalog staging
    python scripts/demos/kubernetes/deploy.py catalog production --back       back to the revision before this run
    python scripts/demos/kubernetes/deploy.py catalog production --previous   back to the previous release (/rollback)

Downloads the package from JFrog Container Registry and checks its SHA-256
against the one recorded at build. Pushes the image and the chart it holds to
docker-local and helm-local, unless JCR has them already, and checks that JCR
reports the same digests. Then:

    helm upgrade --install --rollback-on-failure --wait=watcher --timeout 3m

with the image pinned by digest. If the new pods aren't ready in time, Helm
rolls back to the previous revision by itself, and the old pods, never
stopped, keep serving. Helm's history records that, and nothing else does:
the change board never reads Helm's history. So this script writes it into
the run state (deploys.<environment>.helm_rolled_back_to) and a work note on
the change with both image digests, and exits 0. The check then fails at
once, because the new version isn't live, and the pipeline's rollback runs
--back, which finds Helm's rollback done and confirms it: the change closes
as unsuccessful, like any failed release. A Helm failure without a rollback
exits 1.

Runs on the tools host as the Jenkins service account, with a 10-minute token.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import yaml

# This folder first: its build.py, deploy.py and check.py share names with the Lambda ones in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[2]))
import kube  # noqa: E402
import oci  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, fail, get_parameter, service, ssm_client  # noqa: E402
from publish import download  # noqa: E402


def verified(package, expected):
    actual = hashlib.sha256(package).hexdigest()
    if actual != expected:
        fail(f"The package has SHA-256 {actual}, but the build recorded {expected}. "
             f"Not deploying a package that changed after it was published.")
    print(f"SHA-256 verified: {actual}")


def unpack(package, folder):
    """The package's release.json, with the image and chart layouts beside it in folder."""
    zipfile.ZipFile(io.BytesIO(package)).extractall(folder)
    return json.loads((Path(folder) / "release.json").read_text())


def chart_tag_for(registry, repository, release):
    """The chart's version as its tag, unless JCR holds another chart under it,
    from an earlier build of that version: the pipeline may not overwrite it,
    so this chart then goes under <version>-<digest>, as the image does."""
    version, wanted = release["version"], release["chart"]["digest"]
    existing = registry.tagged(repository, version)
    if existing in (None, wanted):
        return version
    tag = f"{version}-{wanted.split(':')[1][:12]}"
    print(f"{repository}:{version} holds another build's chart ({existing.split(':')[1][:12]}): "
          f"this one goes under {tag}")
    return tag


def push(config, release, folder, token):
    """The image and the chart into JCR, where the cluster pulls from. Idempotent:
    blobs JCR has are skipped, and the tags carry the content, so nothing is overwritten."""
    kube_config = config["kubernetes"]
    registry = oci.Registry(kube_config["registry"], bearer=token)
    image_repo = kube_config["image"].split("/", 1)[1]
    image_tag = f"{release['version']}-{release['image']['digest'].split(':')[1][:12]}"
    image = oci.push_layout(registry, image_repo, Path(folder) / "image", image_tag)
    # A rehearsal of a chart change can push to another path, so it never takes a version's tag.
    chart_repo = os.environ.get("KUBERNETES_CHART_REPOSITORY") or kube_config["chart"]
    chart_tag = chart_tag_for(registry, chart_repo, release)
    chart = oci.push_layout(registry, chart_repo, Path(folder) / "chart", chart_tag)
    if image != release["image"]["digest"] or chart != release["chart"]["digest"]:
        fail("JCR reports other digests than the package names. Not deploying.")
    print(f"Image in JCR: {kube_config['image']}:{image_tag}")
    print(f"        {image}")
    print(f"Chart in JCR: {chart_repo} {chart_tag}")
    print(f"        {chart}")
    # Helm installs the chart from the package, the same bytes JCR now holds.
    _, manifest, blobs = oci.read_layout(Path(folder) / "chart")
    layer = json.loads(manifest)["layers"][0]["digest"]
    chart_file = Path(folder) / release["chart"]["package"]
    chart_file.write_bytes(Path(blobs[layer]).read_bytes())
    return chart_file


def values(config, environment, release):
    env = config["environments"][environment]
    return {"image": {"repository": config["kubernetes"]["image"], "digest": release["image"]["digest"]},
            "environment": environment, "replicas": env["replicas"], "nodePort": env["node_port"],
            "settings": env.get("settings") or {}}


class Watch(threading.Thread):
    """Reads the rollout every 2 seconds while Helm waits: the fewest ready pods
    of the old version, and a line every 30 seconds for the build log."""

    def __init__(self, api, namespace, release, desired, versions):
        super().__init__(daemon=True)
        self.api, self.namespace, self.release, self.desired = api, namespace, release, desired
        self.versions = versions
        self.started = kube.now()
        self.lowest_ready = None
        self.new_pods = set()
        self.stop = threading.Event()

    def run(self):
        last_line = 0
        while not self.stop.wait(2):
            try:
                pods = self.api.pods(self.namespace, self.release)
            except Exception:  # a missed read never stops a deploy
                continue
            counts = {}
            for pod in pods:
                version, ready, deleting, _ = kube.pod_summary(pod)
                if version == self.versions["new"]:
                    self.new_pods.add(pod["metadata"]["name"])
                if not deleting:
                    total, ready_count = counts.get(version, (0, 0))
                    counts[version] = (total + 1, ready_count + ready)
            ready_total = sum(r for _, r in counts.values())
            self.lowest_ready = ready_total if self.lowest_ready is None else min(self.lowest_ready, ready_total)
            elapsed = kube.now() - self.started
            if elapsed - last_line >= 30:
                last_line = elapsed
                parts = ", ".join(f"{v}: {r} of {t} ready" for v, (t, r) in sorted(counts.items()))
                print(f"  {kube.clock(elapsed)} {parts}", flush=True)


def note(state, text):
    """A work note on the change, once the run has one."""
    if state.get("change"):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "change.py"), "note", text], check=False)


def upgrade(config, environment, release, chart_file, folder, kubeconfig, state):
    env = config["environments"][environment]
    name, namespace = config["kubernetes"]["release"], env["namespace"]
    timeout = env.get("helm_timeout", "3m")
    before = kube.history(kubeconfig, name, namespace)
    previous = kube.deployed(before)
    if before and before[-1]["status"].startswith("pending"):
        fail(f"Helm release {name} in {namespace} is {before[-1]['status']}: another deploy didn't finish. "
             f"Run scripts/demos/kubernetes/reset.py, or helm rollback, first.")

    (Path(folder) / "values.yaml").write_text(yaml.safe_dump(values(config, environment, release)))
    args = ["upgrade", "--install", name, chart_file.name, "--namespace", namespace, "--values", "values.yaml",
            "--rollback-on-failure", "--wait=watcher", "--timeout", timeout]
    print(f"Image {config['kubernetes']['image']}@{release['image']['digest']}")
    print(f"{namespace} runs {previous['app_version'] + ' (revision ' + str(previous['revision']) + ')' if previous else 'nothing yet'}")
    print("helm " + " ".join(args), flush=True)

    watch = Watch(kube.Api(kubeconfig), namespace, name, env["replicas"],
                  {"new": release["version"], "old": previous["app_version"] if previous else None})
    watch.start()
    started = kube.now()
    result = kube.helm(kubeconfig, *args, cwd=folder)
    took = kube.now() - started
    watch.stop.set()
    watch.join(5)
    for line in (result.stdout + result.stderr).strip().splitlines():
        print(f"  {line}")

    after = kube.history(kubeconfig, name, namespace)
    current = kube.deployed(after)
    image = f"{config['kubernetes']['image']}@{release['image']['digest']}"
    record = {"namespace": namespace, "previous_number": str(previous["revision"]) if previous else "",
              "previous_version": previous["app_version"] if previous else "", "image": image,
              "image_digest": release["image"]["digest"], "chart_digest": release["chart"]["digest"],
              "url": f"http://127.0.0.1:{env['node_port']}", "lowest_ready": watch.lowest_ready,
              "desired": env["replicas"], "helm_seconds": round(took, 1)}
    deploys = state.setdefault("deploys", {})

    if result.returncode == 0 and current and current["app_version"] == release["version"]:
        record.update({"number": str(current["revision"]), "deployed_at": run_state.now()})
        deploys[environment] = record
        run_state.save(state)
        steady = f" Ready pods never fewer than {watch.lowest_ready} of {env['replicas']}." if previous else ""
        print(f"{namespace}: {name} {release['version']} is revision {current['revision']}, ready after "
              f"{kube.clock(took)}.{steady}")
        note(state, f"Helm: {name} {release['version']} is revision {current['revision']} in {namespace}, all "
             f"{env['replicas']} pods ready.\nImage: {image}\nChart: {release['chart']['repository']} "
             f"{release['version']}, {release['chart']['digest']}")
        return 0

    # Did Helm roll back by itself? Then its newest revision is the old version, deployed again.
    failed = [r for r in after if r["app_version"] == release["version"] and r["status"] == "failed"]
    rolled_back = (previous and current and current["revision"] > previous["revision"]
                   and current["app_version"] == previous["app_version"] and failed)
    timed_out = took >= kube.parse_duration(timeout) - 1
    reason = f"Not ready after {kube.minutes(timeout)}." if timed_out else "Helm upgrade failed."
    record.update({"number": str(failed[-1]["revision"]) if failed else "", "deployed_at": run_state.now()})
    if not rolled_back:
        deploys[environment] = record
        run_state.save(state)
        print(f"{namespace}: {reason} Helm didn't roll back: look at the release now (helm history {name} -n {namespace}).")
        return 1

    old_image = rollback_image(kubeconfig, name, namespace, current["revision"])
    record.update({
        "helm_rolled_back_to": previous["app_version"], "helm_rollback_number": str(current["revision"]),
        "helm_rollback_reason": reason,
        "rollback_notes": (
            f"Helm rolled back: {name} {release['version']} (revision {failed[-1]['revision']}, image {image}) "
            f"was not ready after {kube.minutes(timeout)}. Helm rolled back to revision {current['revision']}, "
            f"{name} {previous['app_version']}{' (image ' + old_image + ')' if old_image else ''}. "
            f"New pods started: {len(watch.new_pods)}. Ready pods never fewer than {watch.lowest_ready} of "
            f"{env['replicas']}: the {previous['app_version']} pods kept running throughout."),
    })
    deploys[environment] = record
    run_state.save(state)
    print(f"{namespace}: {reason} Helm gave up after {kube.clock(took)} and rolled back to revision "
          f"{current['revision']}, {name} {previous['app_version']}.")
    print(f"Ready pods never fewer than {watch.lowest_ready} of {env['replicas']}; "
          f"{release['version']} pods started: {len(watch.new_pods)}.")
    print(f"{release['version']} isn't live: the check fails, and the pipeline records the rollback.")
    note(state, record["rollback_notes"])
    return 0


def rollback_image(kubeconfig, name, namespace, revision):
    """The image a revision runs, from Helm's own record of it."""
    result = kube.helm(kubeconfig, "get", "manifest", name, "--namespace", namespace, "--revision", str(revision))
    for document in yaml.safe_load_all(result.stdout or ""):
        if document and document.get("kind") == "Deployment":
            return document["spec"]["template"]["spec"]["containers"][0]["image"]
    return None


def go_back(config, environment, state, to_previous_release):
    """helm rollback: to this run's previous revision (--back) or the release before the live one (--previous)."""
    env = config["environments"][environment]
    name, namespace = config["kubernetes"]["release"], env["namespace"]
    deploys = state.setdefault("deploys", {})
    with kube.credentials() as kubeconfig:
        revisions = kube.history(kubeconfig, name, namespace)
        current = kube.deployed(revisions)
        if to_previous_release:
            older = [r for r in revisions if current and r["revision"] < current["revision"]
                     and r["app_version"] != current["app_version"] and r["status"] in ("superseded", "deployed")]
            if not older:
                fail(f"{name} in {namespace} has no earlier release to roll back to.")
            target = older[-1]
            deploys[environment] = {"previous_number": str(current["revision"]),
                                    "previous_version": current["app_version"], "namespace": namespace}
        else:
            record = deploys.get(environment, {})
            if not record.get("previous_number"):
                fail(f"This run found nothing in {namespace} to go back to.")
            target = next(r for r in revisions if str(r["revision"]) == record["previous_number"])
            if record.get("helm_rolled_back_to") and current and current["app_version"] == target["app_version"]:
                # Helm rolled back by itself during the deploy: confirm it, don't roll back twice.
                deploys[environment].update({"rolled_back_to": target["app_version"],
                                             "rolled_back_number": str(current["revision"]),
                                             "rolled_back_by": "Helm"})
                run_state.save(state)
                print(f"{namespace}: Helm rolled back by itself to revision {current['revision']}, "
                      f"{name} {target['app_version']}. Nothing more to roll back.")
                note(state, f"Rollback: Helm had rolled back by itself, with --rollback-on-failure, to revision "
                            f"{current['revision']}, {name} {target['app_version']}. {record['rollback_notes']}")
                return
        args = ["rollback", name, str(target["revision"]), "--namespace", namespace, "--wait=watcher",
                "--timeout", env.get("helm_timeout", "3m")]
        print("helm " + " ".join(args), flush=True)
        result = kube.helm(kubeconfig, *args)
        print((result.stdout + result.stderr).strip())
        if result.returncode != 0:
            fail(f"helm rollback in {namespace} failed.")
        now_current = kube.deployed(kube.history(kubeconfig, name, namespace))
    deploys[environment].update({"rolled_back_to": target["app_version"], "rolled_back_number": str(now_current["revision"]),
                                 "rolled_back_by": "the pipeline", "url": f"http://127.0.0.1:{env['node_port']}"})
    run_state.save(state)
    print(f"{namespace}: back on {name} {target['app_version']}, revision {now_current['revision']}")


def deploy(config, environment, package, state, token):
    """Deploy a package already downloaded and checked. Returns the exit code."""
    with tempfile.TemporaryDirectory(prefix="deploy-") as folder:
        release = unpack(package, folder)
        if release["version"] != state["version"]:
            fail(f"The package holds {release['version']}, this run releases {state['version']}.")
        chart_file = push(config, release, folder, token)
        with kube.credentials() as kubeconfig:
            return upgrade(config, environment, release, chart_file, folder, kubeconfig, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    back = parser.add_mutually_exclusive_group()
    back.add_argument("--back", action="store_true", help="helm rollback to where this run found it")
    back.add_argument("--previous", action="store_true", help="helm rollback to the previous release")
    args = parser.parse_args()

    config = service(args.service)
    state = run_state.load()
    if args.back or args.previous:
        go_back(config, args.environment, state, args.previous)
        return 0
    token = get_parameter(ssm_client(), "jcr/token")
    package = download(token, state["package"]["path"])
    verified(package, state["package"]["sha256"])
    return deploy(config, args.environment, package, state, token)


if __name__ == "__main__":
    sys.exit(main())
