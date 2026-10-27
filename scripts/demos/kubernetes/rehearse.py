"""Rehearse demo 8 on the real cluster and check its acceptance criteria.

    python scripts/demos/kubernetes/rehearse.py      from the Mac, through SSM, about 8 minutes

Runs the video's story with the pipeline's own build, deploy and check
scripts, on the tools host, without Slack, ServiceNow or Jira: 1.9.0 passes
staging, is never ready in production and Helm rolls it back; then 1.9.1
goes out. A prober asks production's /health five times a second during each
production deploy. Then the access checks, the memory, and reset.py twice.
The packages are built on the host and deployed from there, as the pipeline
deploys the one it downloads from JCR. Ends with the cluster on 1.8.2.

Prints one line per acceptance check: ok or FAIL.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import yaml

os.environ.setdefault("WORKSPACE", tempfile.mkdtemp(prefix="rehearse-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import deploy  # noqa: E402
import kube  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, get_parameter, service, ssm_client  # noqa: E402

SERVICE = "catalog"
HERE = Path(__file__).resolve().parent
results = []


def result(ok, what):
    results.append(ok)
    print(f"{'ok  ' if ok else 'FAIL'} {what}", flush=True)


class Prober(threading.Thread):
    """GET /health five times a second; counts answers per version, and failures."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.versions, self.failures = url, {}, 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(0.2):
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=2) as response:
                    version = json.loads(response.read())["version"]
                    self.versions[version] = self.versions.get(version, 0) + 1
            except Exception:
                self.failures += 1

    def done(self):
        self.stop.set()
        self.join(3)
        return self.versions, self.failures


def release(version, commit, token):
    """Build a demo state and start a run state for it, as run_state.py init and publish.py would."""
    out = tempfile.mkdtemp(prefix=f"build-{version}-")
    info = build.build(SERVICE, version_dir=ROOT / "demo-states" / SERVICE / version, out=out, commit=commit)
    package = (Path(out) / SERVICE / info["package"]).read_bytes()
    state = {"service": SERVICE, "version": version, "build": {"number": "rehearsal"},
             "package": {"path": f"(rehearsal) {info['package']}", "sha256": info["sha256"]}}
    run_state.save(state)
    print(f"== {SERVICE} {version}: {info['image']}", flush=True)
    return package, state, info


def check(environment, version):
    state = run_state.load()
    url = state["deploys"][environment]["url"]
    return subprocess.run([sys.executable, str(HERE / "check.py"), SERVICE, environment, "--version", version,
                           "--url", url]).returncode == 0


def history_lines(environment):
    """What the video's helm history line prints: revision, app version, status, description."""
    config = service(SERVICE)
    with kube.credentials(quiet=True) as kubeconfig:
        revisions = kube.history(kubeconfig, config["kubernetes"]["release"],
                                 config["environments"][environment]["namespace"])
    for r in revisions[-3:]:
        print(f"    {r['revision']}  {r['app_version']}  {r['status']:<10} {r['description'][:24]}")
    return revisions


def can_i():
    """The Jenkins account's own token, asked what it may do."""
    with kube.credentials(quiet=True) as kubeconfig:
        env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
        ask = lambda *args: subprocess.run(["kubectl", "auth", "can-i", *args], env=env, capture_output=True,
                                           text=True).stdout.strip()
        allowed = [ask("patch", "deploy", "-n", ns) for ns in ("staging", "production")]
        refused = [ask("patch", "deploy", "-n", "default"), ask("patch", "deploy", "-A"),
                   ask("create", "namespaces"), ask("get", "secrets", "-n", "kube-system"),
                   ask("get", "secrets", "-n", "jenkins"), ask("create", "clusterrolebindings")]
        claims = kube.token_claims(yaml.safe_load(Path(kubeconfig).read_text())["users"][0]["user"]["token"])
    result(allowed == ["yes", "yes"] and set(refused) == {"no"},
           f"3. Jenkins may patch deployments in staging and production ({', '.join(allowed)}), "
           f"nothing elsewhere ({', '.join(refused)})")
    result(claims["exp"] - claims["iat"] == 600, f"   the build's token lasts {(claims['exp'] - claims['iat']) // 60} minutes")


def memory():
    current = lambda p: int(Path(f"/sys/fs/cgroup/{p}/memory.current").read_text()) >> 20
    limit = lambda p: int(Path(f"/sys/fs/cgroup/{p}/memory.max").read_text()) >> 20
    used = current("system.slice/k3s.service") + current("kubepods.slice")
    cap = limit("system.slice/k3s.service") + limit("kubepods.slice")
    result(cap <= 2048, f"5. the cluster may use at most {cap} MiB (k3s and its pods); it uses {used} MiB now")


def main():
    commit = sys.argv[1] if len(sys.argv) > 1 else "rehearsal"
    started = time.monotonic()
    config = service(SERVICE)
    token = get_parameter(ssm_client(), "jcr/token")
    production_url = f"http://127.0.0.1:{config['environments']['production']['node_port']}"

    subprocess.run([sys.executable, str(HERE / "reset.py"), "--here", "--commit", commit], check=True)

    package, state, info = release("1.9.0", commit, token)
    result(deploy.deploy(config, "staging", package, state, token) == 0 and check("staging", "1.9.0"),
           "   1.9.0 passes staging")
    prober = Prober(production_url)
    prober.start()
    code = deploy.deploy(config, "production", package, run_state.load(), token)
    versions, failures = prober.done()
    record = run_state.load()["deploys"]["production"]
    result(code == 0 and record.get("helm_rolled_back_to") == "1.8.2",
           f"2. 1.9.0 never ready in production: Helm rolled back to {record.get('helm_rolled_back_to')} after "
           f"{kube.clock(record['helm_seconds'])} ({record.get('helm_rollback_reason')})")
    result(not check("production", "1.9.0"), "   the production check fails at once, as the pipeline needs")
    back = subprocess.run([sys.executable, str(HERE / "deploy.py"), SERVICE, "production", "--back"])
    record = run_state.load()["deploys"]["production"]
    result(back.returncode == 0 and record.get("rolled_back_to") == "1.8.2" and record.get("rolled_back_by") == "Helm",
           "   the pipeline's rollback (deploy.py --back) confirms Helm's, without a second one")
    result(set(versions) == {"1.8.2"} and failures == 0,
           f"2. the prober saw only 1.8.2 throughout: {versions}, {failures} failed requests")
    result(record["lowest_ready"] == 3, f"1. ready pods in production never fewer than {record['lowest_ready']} of 3")
    print("   helm history (the video's line):")
    revisions = history_lines("production")
    result([r["status"] for r in revisions[-2:]] == ["failed", "deployed"]
           and revisions[-1]["description"].startswith("Rollback to"), "   Helm's history shows the failed 1.9.0 and the rollback")
    print(f"   the change's notes would read: {record['rollback_notes']}")
    result(check("production", "1.8.2"), "   production answers on 1.8.2 after the rollback")

    package, state, info = release("1.9.1", commit, token)
    result(deploy.deploy(config, "staging", package, state, token) == 0 and check("staging", "1.9.1"),
           "   1.9.1 passes staging")
    prober = Prober(production_url)
    prober.start()
    code = deploy.deploy(config, "production", package, run_state.load(), token)
    versions, failures = prober.done()
    record = run_state.load()["deploys"]["production"]
    result(code == 0 and record["lowest_ready"] >= 3,
           f"1. 1.9.1 replaced the pods without dropping below 3 ready (lowest {record['lowest_ready']}) "
           f"in {kube.clock(record['helm_seconds'])}")
    result(failures == 0, f"   the prober saw {versions}, {failures} failed requests")
    result(check("production", "1.9.1") and record["image_digest"] == info["image_digest"],
           f"4. production runs {info['image_digest'][:19]}..., the digest in build-info.json and the change")

    can_i()
    memory()

    for attempt in ("first", "second"):
        begun = time.monotonic()
        ok = subprocess.run([sys.executable, str(HERE / "reset.py"), "--here", "--commit", commit]).returncode == 0
        took = time.monotonic() - begun
        result(ok and took < 120, f"6. reset.py, {attempt} run: production back on 1.8.2 in {kube.clock(took)}")

    print(f"Rehearsal done in {kube.clock(time.monotonic() - started)}: "
          f"{'all checks ok' if all(results) else f'{results.count(False)} FAILED'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    if "--here" in sys.argv:
        sys.argv.remove("--here")
        main()
    else:
        import host
        from common import git

        commit = git("rev-parse", "HEAD", check=False).stdout.strip() or "rehearsal"
        code, output = host.run(f"python3 scripts/demos/kubernetes/rehearse.py --here {commit}", timeout=1800)
        print(output, end="")
        sys.exit(code)
