"""Stages 6 and 10, Staging and Production: one image to three clouds.

    python scripts/demos/clouds/deploy.py quote-service staging
    python scripts/demos/clouds/deploy.py quote-service production
    python scripts/demos/clouds/deploy.py quote-service production --back       the failed cloud back, after a failed check
    python scripts/demos/clouds/deploy.py quote-service production --previous   every cloud on the newest version back (/rollback)

Every deploy starts the same way:

1. The package from JFrog Container Registry, its SHA-256 checked against the
   build's. It holds the image the build stage made, once.
2. Each cloud trades the build's ID token for its own short-lived access
   (scripts/demos/clouds/identity.py). Jenkins stores no cloud keys.
3. Staging pushes the image to JFrog's Docker repository and copies it by
   digest to Amazon ECR, Azure Container Registry and Artifact Registry.
   Then the digest check: all four must hold the build's digest, or nothing
   is deployed. Production checks the same four again.

Staging then goes to all three clouds. Production goes one cloud at a time,
in the order services.yaml gives (Google Cloud, Azure, AWS), and checks each
before the next. A failed check stops there: the clouds after it are not
touched, and the pipeline's check and rollback move only the failed one back.

The run state keeps each cloud's revision, version and address under
deploys.<environment>.clouds, which check.py and --back read.
CLOUDS=aws limits a run to some clouds, for trying one cloud on its own.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

import run_state  # noqa: E402
from check import check_cloud, cloud_names  # noqa: E402
from common import ROOT, fail, get_parameter, service, ssm_client  # noqa: E402
from identity import SignInError, describe_token, id_token, sign_in  # noqa: E402
from registry import Layout, Registry, RegistryError, copy  # noqa: E402
from targets import CloudError, cloud_settings, target  # noqa: E402

JCR_DOCKER_REPOSITORY = "docker-local"
HERE = Path(__file__).resolve().parent


def clouds_in_order(config):
    """The clouds this run touches, in production order. CLOUDS narrows them."""
    order = [c["key"] for c in config["clouds"]]
    only = [c.strip() for c in os.environ.get("CLOUDS", "").split(",") if c.strip()]
    if only:
        unknown = set(only) - set(order)
        if unknown:
            fail(f"CLOUDS names {', '.join(sorted(unknown))}; services.yaml has {', '.join(order)}")
        print(f"Only {', '.join(only)} in this run (CLOUDS is set)")
        order = [c for c in order if c in only]
    return order


def label(state):
    """What tells this run's revisions and tags apart: b17 for Jenkins build 17.
    Kept in the run state, so staging and production use the same one."""
    if not state.get("image", {}).get("label"):
        build = str(state.get("build", {}).get("number", "local"))
        state.setdefault("image", {})["label"] = f"b{build}" if build.isdigit() else f"local{int(time.time()) % 1000000}"
    return state["image"]["label"]


# The image -------------------------------------------------------------------

def package_layout(state, workspace):
    """The published package from JFrog, checked, unpacked: the image as the build made it."""
    from publish import download

    token = get_parameter(ssm_client(), "jcr/token")
    package = download(token, state["package"]["path"])
    actual = hashlib.sha256(package).hexdigest()
    if actual != state["package"]["sha256"]:
        fail(f"{state['package']['path']} has SHA-256 {actual}, but the build recorded {state['package']['sha256']}. "
             f"Not deploying a package that changed after it was published.")
    print(f"Package {state['package']['name']} from JFrog, SHA-256 verified: {actual}")
    directory = workspace / ".release" / "deploy-package"
    if directory.exists():
        for path in sorted(directory.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
    zipfile.ZipFile(io.BytesIO(package)).extractall(directory)
    layout = Layout(directory / "image")
    digest = layout.image()["digest"]
    layout.manifest("", digest)  # reads the manifest blob and checks it against its digest
    return layout, digest


def jcr_registry():
    url = os.environ.get("JCR_URL", "http://localhost:8082")
    host = url.split("://", 1)[1].rstrip("/")
    return Registry(host, "release-pipeline", get_parameter(ssm_client(), "jcr/token"), name="JFrog",
                    scheme=url.split("://", 1)[0])


def registries(targets):
    """(registry, repository) for JFrog and each cloud, in the order the log shows them."""
    found = [(jcr_registry(), f"{JCR_DOCKER_REPOSITORY}/{targets[0].config['name']}")]
    for t in sorted(targets, key=lambda t: ["aws", "azure", "gcp"].index(t.key)):
        found.append((t.registry(), t.settings["repository"]))
    return found


def distribute(layout, digest, tag, places):
    """Push the build's image to JFrog, then copy it by digest from JFrog to each cloud."""
    (jcr, jcr_repository), *clouds = places
    print(f"Image {digest}, tag {tag}")
    copy(layout, "", digest, jcr, jcr_repository, tag)
    for registry, repository in clouds:
        copy(jcr, jcr_repository, digest, registry, repository, tag)


def digest_check(digest, tag, places):
    """Every registry must hold the build's digest under this run's tag. Nothing deploys otherwise."""
    print(f"Digest check of {tag}, before any deploy:")
    width = max(len(r.name) for r, _ in places)
    wrong = []
    for registry, repository in places:
        found = registry.digest(repository, tag)
        ok = found == digest
        wrong += [] if ok else [registry.name]
        print(f"  {'ok  ' if ok else 'FAIL'} {registry.name:<{width}}  {found or 'no such tag'}")
    if wrong:
        fail(f"Digest check failed: {', '.join(wrong)} don't hold {digest}. Nothing was deployed.")
    print(f"All {len(places)} registries hold the image the build made once: {digest}")


# Signing in ------------------------------------------------------------------

def sign_in_all(config, clouds, environment):
    """One login per cloud, from the build's ID tokens, shown as the token step."""
    names = cloud_names(config)
    settings = {c: cloud_settings(c, config) for c in clouds}
    token = next((id_token(c) for c in clouds if id_token(c)), None)
    print(describe_token(token) if token else "No ID token: signing in with your own logins (local run)")
    logins = {}
    for cloud in clouds:
        try:
            logins[cloud] = sign_in(cloud, settings[cloud])
        except SignInError as error:
            fail(str(error))
        print(logins[cloud].line(names[cloud]))
    if token:
        print("No cloud keys are stored anywhere: each cloud trusts the build's ID token, and the access it gave expires on its own.")
    return {c: target(c, config, environment, settings[c], logins[c]) for c in clouds}


# Deploying -------------------------------------------------------------------

def record(target_, revision, previous, url):
    return {"name": target_.name, "url": url, "status": "released", "revision": revision.id,
            "version": revision.version, "previous_revision": previous.id, "previous_version": previous.version,
            "expected": revision.version, "check": None}


def untouched(target_, live, url):
    return {"name": target_.name, "url": url, "status": "not released", "revision": live.id, "version": live.version,
            "previous_revision": live.id, "previous_version": live.version, "expected": live.version, "check": None}


def summary(clouds_state):
    notes = {"rolled back": " (rolled back)", "not released": " (not released)", "failed check": " (failed its check)"}
    return ", ".join(f"{c['name']} {c['expected']}{notes.get(c['status'], '')}" for c in clouds_state.values())


def deploy_environment(config, environment, targets, digest, version, run_label):
    """Staging: every cloud. Production: cloud by cloud, each checked before the next."""
    env = config["environments"][environment]
    clouds_state = {}
    stopped_at = None
    for t in targets:
        url = t.url()
        live = t.live()
        if stopped_at:
            clouds_state[t.key] = untouched(t, live, url)
            print(f"{t}: not released, the release stopped at {stopped_at}. Still on {live.version}.")
            continue
        started = time.monotonic()
        revision = t.deploy(digest, version, run_label)
        clouds_state[t.key] = record(t, revision, live, url)
        print(f"{t}: {revision.id} ({version}) is live in {time.monotonic() - started:.0f}s, was {live.id} ({live.version})")
        if environment == "production":
            if check_cloud(url, t.key, t.name, environment, version, env["check"]["paths"], env["check"]["timeout_seconds"]):
                clouds_state[t.key]["check"] = "ok"
            else:
                clouds_state[t.key].update(check="failed", status="failed check")
                stopped_at = t.name
                print(f"{t} failed its check: stopping here, before the next cloud.")
    return clouds_state, stopped_at


def note_change(text):
    """A work note on the run's change, once it exists. change.py does nothing before then."""
    subprocess.run([sys.executable, str(ROOT / "scripts" / "change.py"), "note", text], cwd=ROOT, check=False)


def link_cloud_items(config, state):
    """The change lists quote-service in each cloud as an affected item."""
    if not state.get("change"):
        return
    result = subprocess.run([sys.executable, str(HERE / "servicenow_items.py"), "link", config["name"]], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Affected items not added to the change: {result.stderr.strip()[-200:]}")


def start_prober(state_path):
    """scripts/demos/clouds/prober.py, for Grafana: every /health every 5 seconds until the run ends.
    It must outlive this step, so Jenkins is told not to stop it with the step."""
    pid_file = state_path.parent / "prober.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 0)
            return
        except (OSError, ValueError):
            pass
    env = {**os.environ, "JENKINS_NODE_COOKIE": "dontKillMe", "BUILD_ID": "dontKillMe"}
    with open(state_path.parent / "prober.log", "ab") as log:
        process = subprocess.Popen([sys.executable, str(HERE / "prober.py"), "--follow", str(state_path)],
                                   cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    pid_file.write_text(str(process.pid))
    print(f"Prober started for Grafana (pid {process.pid})")


def deploy(config, environment, clouds, state):
    workspace = Path(os.environ.get("WORKSPACE", ROOT))
    version = state["version"]
    run_label = label(state)
    tag = f"{version}-{run_label.removeprefix('b')}"
    layout, digest = package_layout(state, workspace)
    info = workspace / "dist" / config["name"] / "build-info.json"
    expected = json.loads(info.read_text()).get("image", {}).get("digest") if info.exists() else None
    if expected and expected != digest:
        fail(f"The package holds image {digest}, but this run's build made {expected}.")
    state["image"].update(digest=digest, tag=tag)

    targets = list(sign_in_all(config, clouds, environment).values())
    places = registries(targets)
    if environment == "staging":
        distribute(layout, digest, tag, places)
    digest_check(digest, tag, places)
    if environment == "staging":
        start_prober(run_state.PATH)

    clouds_state, stopped_at = deploy_environment(config, environment, targets, digest, version, run_label)
    failed = [c for c in clouds_state.values() if c["check"] == "failed"]
    reference = failed[0] if failed else next(iter(clouds_state.values()))
    state.setdefault("deploys", {})[environment] = {
        "number": digest,
        "previous_number": ", ".join(f"{c['name']} {c['previous_revision']}" for c in clouds_state.values()),
        "previous_version": reference["previous_version"],
        "url": ",".join(c["url"] for c in clouds_state.values()),
        "deployed_at": run_state.now(),
        "clouds": clouds_state,
        "summary": summary(clouds_state),
    }
    run_state.save(state)
    print(f"{environment.capitalize()} now: {summary(clouds_state)}")
    if environment == "production":
        link_cloud_items(config, state)
        note_change(f"Production, cloud by cloud: {summary(clouds_state)}. Image {digest}.")


def back(config, environment, clouds, state):
    """After a failed check: the clouds that failed go back to where this run found
    them. If none is marked failed, every cloud this run released goes back."""
    deployed = state["deploys"][environment]
    infos = deployed["clouds"]
    failed = [c for c in clouds if infos.get(c, {}).get("check") == "failed"]
    moving = failed or [c for c in clouds if infos.get(c, {}).get("status") == "released"]
    if not moving:
        fail(f"Nothing to move back in {environment}: this run released no cloud there.")
    targets = sign_in_all(config, moving, environment)
    for cloud in moving:
        info = infos[cloud]
        started = time.monotonic()
        targets[cloud].move(info["previous_revision"])
        info.update(status="rolled back", expected=info["previous_version"], version=info["previous_version"],
                    revision=info["previous_revision"])
        print(f"{targets[cloud]}: back on {info['previous_revision']} ({info['previous_version']}) in "
              f"{time.monotonic() - started:.0f}s")
    first = infos[moving[0]]
    deployed.update(rolled_back_to=first["previous_version"], rolled_back_number=first["previous_revision"],
                    previous_version=first["previous_version"], summary=summary(infos))
    run_state.save(state)
    print(f"{environment.capitalize()} now: {summary(infos)}")
    note_change(f"Rolled back in {', '.join(infos[c]['name'] for c in moving)}. Each cloud now runs: {summary(infos)}.")


def semver(version):
    return tuple(int(p) for p in version.split(".")) if version != "unknown" else (-1,)


def previous(config, environment, clouds, state):
    """/rollback: every cloud on the newest live version goes to the release before it.
    Clouds already on an older version stay where they are."""
    targets = sign_in_all(config, clouds, environment)
    lives = {c: targets[c].live() for c in clouds}
    newest = max((live.version for live in lives.values()), key=semver)
    infos = {}
    moved_to = None
    for cloud in clouds:
        t, live = targets[cloud], lives[cloud]
        url = t.url()
        if live.version != newest:
            infos[cloud] = untouched(t, live, url)
            print(f"{t}: stays on {live.version}")
            continue
        earlier = t.previous_release(live)
        if earlier is None:
            fail(f"{t} has no earlier release to roll back to.")
        t.move(earlier.id)
        moved_to = moved_to or earlier.version
        infos[cloud] = {**record(t, earlier, live, url), "status": "rolled back"}
        print(f"{t}: back on {earlier.id} ({earlier.version}), was {live.id} ({live.version})")
    state.setdefault("deploys", {})[environment] = {
        "previous_number": ", ".join(f"{i['name']} {i['previous_revision']}" for i in infos.values()),
        "previous_version": newest, "rolled_back_to": moved_to,
        "rolled_back_number": ", ".join(f"{i['name']} {i['revision']}" for i in infos.values()),
        "url": ",".join(i["url"] for i in infos.values()), "clouds": infos, "summary": summary(infos)}
    run_state.save(state)
    print(f"{environment.capitalize()} now: {summary(infos)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    move = parser.add_mutually_exclusive_group()
    move.add_argument("--back", action="store_true", help="the failed cloud back to where this run found it")
    move.add_argument("--previous", action="store_true", help="every cloud on the newest version to the release before")
    args = parser.parse_args()

    config = service(args.service)
    if args.environment not in config["environments"]:
        fail(f"{args.service} has no environment {args.environment}")
    clouds = clouds_in_order(config)
    state = run_state.load()
    try:
        if args.back:
            back(config, args.environment, clouds, state)
        elif args.previous:
            previous(config, args.environment, clouds, state)
        else:
            deploy(config, args.environment, clouds, state)
    except (CloudError, RegistryError, SignInError) as error:
        fail(str(error))


if __name__ == "__main__":
    sys.exit(main())
