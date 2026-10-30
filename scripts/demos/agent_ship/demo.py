"""scripts/demo.sh up, down and status for video 16, the coding agent that can't ship.

    python scripts/demos/agent_ship/demo.py up       its root, the image on the tools host, the rulesets on
    python scripts/demos/agent_ship/demo.py down     the rulesets off, demo 14's workflow back on
    python scripts/demos/agent_ship/demo.py status   the rulesets, the app's rights, the image, the pull-requests job

up applies infra/demos/16-agent-ship (Terraform shows its plan and asks
first), builds the coding-agent image on the tools host, turns video 16's
rulesets on, and disables demo 14's release workflow if it exists, so Sam's
merge starts no GitHub Actions release beside the Slack one. down undoes
the last two: with the rulesets on, video 14's merge box would show a bypass
and demo 2's pull requests would need a review. The rulesets and the
workflow need your gh login. Nothing here runs by the hour.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402

WORKFLOW = ".github/workflows/release.yml"


def gh(*args, check=True):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)}: {result.stderr.strip()}")
    return result


def workflow_exists(repository):
    return gh("api", f"repos/{repository}/contents/{WORKFLOW}", check=False).returncode == 0


def image_tag():
    import agent_task

    return agent_task.agent_access().image_tag()


def build_on_host():
    import host

    tag = image_tag()
    script = (f"set -e\ncd {host.REPO_DIR}\ngit pull --ff-only -q || true\n"
              f"docker image inspect {tag} >/dev/null 2>&1 && echo \"{tag}: built already\" && exit 0\n"
              f"docker build -q -t {tag} -f jenkins/toolchain/coding-agent.Dockerfile jenkins/toolchain/coding-agent\n"
              f"docker run --rm --entrypoint claude {tag} --version")
    code, out, err = host.Host().shell(script, timeout=1500)
    print(out.strip() or err.strip())
    if code != 0:
        raise SystemExit("The image didn't build on the tools host: see above.")


def up():
    subprocess.run([str(HERE / "tf.sh"), "apply"], check=True)
    build_on_host()
    subprocess.run([sys.executable, str(HERE / "ruleset.py"), "--on"], check=True)
    repository = get_parameter(ssm_client(), "github/repository")
    if workflow_exists(repository):
        gh("workflow", "disable", "release.yml", "--repo", repository)
        print(f"Demo 14's {WORKFLOW}: disabled while video 16 is up")
    print("Ready: run scripts/demos/agent_ship/check.sh, then scripts/reset.py agent-ship")


def down():
    subprocess.run([sys.executable, str(HERE / "ruleset.py"), "--off"], check=True)
    repository = get_parameter(ssm_client(), "github/repository")
    if workflow_exists(repository):
        gh("workflow", "enable", "release.yml", "--repo", repository)
        print(f"Demo 14's {WORKFLOW}: enabled again")
    print("Down: the rulesets are off (kept, not deleted). Nothing of video 16 costs money by the hour.")


def status():
    ssm = ssm_client()
    repository = get_parameter(ssm, "github/repository")
    import ruleset

    found = ruleset.existing(repository)
    for name in (ruleset.MAIN, ruleset.BRANCHES, ruleset.TAGS):
        print(f"  ruleset \"{name}\": {found[name]['enforcement'] if name in found else 'not created'}")
    try:
        installation = agent.installation(ssm, repository)
        print(f"  app {installation['app_slug']}: installed ({installation['repository_selection']} repositories), "
              f"permissions {json.dumps(dict(sorted(installation['permissions'].items())))}")
    except SystemExit:
        print("  the coding agent's app: not created or not installed (agent_app.py)")
    import host

    tag = image_tag()
    code, out, _ = host.Host().shell(
        f"docker run --rm --entrypoint claude {tag} --version 2>/dev/null || echo 'not built'\n"
        # -g: without it curl reads tree=lastBuild[...] as a URL pattern and fetches nothing.
        "curl -s -g -u \"admin:$(cat /run/release-demo/secrets/jenkins-admin-password)\" "
        "'http://localhost:8080/job/pull-requests/api/json?tree=lastBuild[displayName,result,description]'")
    lines = out.strip().splitlines()
    print(f"  image {tag} on the tools host: Claude Code {lines[0] if lines else 'unknown'}")
    last = json.loads(lines[1]).get("lastBuild") if len(lines) > 1 and lines[1].startswith("{") else None
    print(f"  pull-requests job: {(last or {}).get('displayName', 'no build yet')} "
          f"{(last or {}).get('result') or ''} {(last or {}).get('description') or ''}".rstrip())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    args = parser.parse_args()
    {"up": up, "down": down, "status": status}[args.action]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
