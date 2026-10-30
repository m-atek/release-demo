"""GitHub's main to GitLab's main: the copy GitLab CI releases from.

    python scripts/demos/gitlab_port/sync.py            push GitHub's main to GitLab's, fast-forward only
    python scripts/demos/gitlab_port/sync.py --check    both mains' commits, nothing pushed

On GitLab.com Free pull mirroring is a paid feature, so main is pushed from
here, over HTTPS with the demo account's token. git's credential helper
reads the token from the environment, as host.py does, so it stays out of
files and ps. Only a fast-forward: if GitLab's main has moved on its own, it
stops and asks Mateusz, and never force-pushes.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from gitlab_demo import GitLab, GitLabError, fail, get_parameter

CREDENTIALS = '!f() { echo "username=$GITLAB_USER"; echo "password=$GITLAB_TOKEN"; }; f'


def github_main(repository):
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repository}.git", "refs/heads/main"],
                         capture_output=True, text=True, check=True).stdout
    return out.split()[0] if out.strip() else ""


def gitlab_main(gl, project):
    try:
        return gl.call("GET", f"/projects/{project['id']}/repository/branches/main")["commit"]["id"]
    except GitLabError as error:
        if error.status == 404:
            return ""  # an empty project: the first push makes main
        raise


def push(gl, project, repository):
    env = {**os.environ, "GITLAB_USER": gl.username(), "GITLAB_TOKEN": gl.token, "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="release-demo-sync-") as temp:
        clone = Path(temp) / "release-demo.git"
        subprocess.run(["git", "clone", "--quiet", "--bare", "--single-branch", "--branch", "main",
                        f"https://github.com/{repository}.git", str(clone)], check=True, env=env)
        result = subprocess.run(["git", "-C", str(clone), "-c", f"credential.helper={CREDENTIALS}", "push", "--quiet",
                                 f"https://gitlab.com/{project['path_with_namespace']}.git", "main:refs/heads/main"],
                                capture_output=True, text=True, env=env)
    if result.returncode != 0:
        if "non-fast-forward" in result.stderr or "fetch first" in result.stderr:
            fail("GitLab's main has commits GitHub's main doesn't: not pushing over them. Ask Mateusz before "
                 "anything is force-pushed.")
        fail(f"git push to GitLab failed: {result.stderr.strip()[-300:]}")


def sync(gl=None, check_only=False):
    gl = gl or GitLab()
    project = gl.project()
    repository = get_parameter(gl.ssm, "github/repository")
    source, target = github_main(repository), gitlab_main(gl, project)
    print(f"GitHub's main {source[:7] or 'none'}, GitLab's main {target[:7] or 'none'}")
    if source == target:
        print("In sync.")
        return 0
    if check_only:
        print("Not in sync: scripts/demos/gitlab_port/sync.py pushes it.")
        return 1
    push(gl, project, repository)
    print(f"Pushed {source[:7]} to GitLab's main.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare the two mains, push nothing")
    args = parser.parse_args()
    return sync(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
