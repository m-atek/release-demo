"""Video 16's acceptance checks 2 and 3, inside the coding agent's container, with an agent token.

    python3 /checks/container_checks.py --repository m-atek/release-demo --stamp 20261102T1302 --blocked 172.18.0.3:8082,...

Run by check.py on the tools host, in the coding-agent image on the network
release-demo-agent, with a clone at /work/release-demo, never by hand. It
pushes and opens a test pull request as the coding agent's app, so it
changes GitHub: a branch agent/check-<stamp> and pull request "Check by
check.py", closed at the end. Branches stay (deleting one asks you first).
One ok or FAIL line each; no value is printed.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys

WORK = "/work/release-demo"


def run(*args):
    result = subprocess.run(list(args), capture_output=True, text=True, cwd=WORK)
    return result.returncode, result.stdout + result.stderr


def line(ok, text):
    print(f"{'ok  ' if ok else 'FAIL'} {text}", flush=True)
    return ok


def refused_by_rule(output, ref):
    return f"GH013: Repository rule violations found for {ref}" in output


def github_checks(repository, stamp):
    results = []
    run("git", "commit", "--allow-empty", "-q", "-m", f"Check by check.py {stamp}")
    code, out = run("git", "push", "origin", "HEAD:main")
    results.append(line(code != 0 and refused_by_rule(out, "refs/heads/main"), "2. a push to main fails with GH013"))
    code, out = run("git", "push", "origin", "HEAD:refs/heads/feature/x")
    results.append(line(code != 0 and refused_by_rule(out, "refs/heads/feature/x"), "2. a push to feature/x fails"))
    run("git", "tag", f"check-{stamp}")
    code, out = run("git", "push", "origin", f"refs/tags/check-{stamp}")
    results.append(line(code != 0 and refused_by_rule(out, f"refs/tags/check-{stamp}"), "2. a tag push fails"))
    branch = f"agent/check-{stamp}"
    code, out = run("git", "push", "origin", f"HEAD:refs/heads/{branch}")
    results.append(line(code == 0, f"2. a push to {branch} succeeds"))
    code, out = run("gh", "pr", "create", "--repo", repository, "--head", branch, "--base", "main",
                    "--title", f"Check by check.py {stamp}", "--body", "Video 16's acceptance check 2. Closed by it.")
    found = re.search(r"/pull/(\d+)", out)
    results.append(line(code == 0 and bool(found), "2. gh pr create succeeds"))
    number = found.group(1) if found else "0"
    code, out = run("gh", "pr", "review", number, "--repo", repository, "--approve")
    results.append(line(code != 0 and re.search(r"[Cc]an ?not approve your own pull request", out) is not None,
                        "2. gh pr review --approve on its own pull request fails"))
    code, out = run("gh", "pr", "merge", number, "--repo", repository, "--squash")
    results.append(line(code != 0, "2. gh pr merge fails"))
    code, sha = run("git", "rev-parse", "HEAD")
    code, out = run("gh", "api", f"repos/{repository}/check-runs", "-f", "name=check", "-f", f"head_sha={sha.strip()}")
    results.append(line(code != 0 and "403" in out, "2. creating a check run fails with 403"))
    os.makedirs(f"{WORK}/.github/workflows", exist_ok=True)
    with open(f"{WORK}/.github/workflows/check-{stamp}.yml", "w") as file:
        file.write("on: push\njobs: {}\n")
    run("git", "add", ".github/workflows")
    run("git", "commit", "-q", "-m", "A workflow, which the app may not push")
    code, out = run("git", "push", "origin", f"HEAD:refs/heads/{branch}-workflow")
    results.append(line(code != 0 and "workflow" in out.lower(), "2. a push that changes .github/workflows/ fails"))
    run("gh", "pr", "close", number, "--repo", repository, "--comment", "Closed by check.py.")
    print(f"     test pull request #{number} closed; the branches {branch} and {branch}-workflow stay")
    return all(results)


def unreachable(blocked):
    results = []
    for target in [t for t in blocked.split(",") if t]:
        host, port = target.rsplit(":", 1)
        try:
            socket.create_connection((host, int(port)), timeout=3).close()
            results.append(line(False, f"3. {target} answers from the agent's network"))
        except OSError:
            results.append(line(True, f"3. {target} doesn't answer"))
    return all(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--blocked", default="")
    parser.add_argument("--skip-github", action="store_true")
    args = parser.parse_args()
    results = [unreachable(args.blocked)]
    if not args.skip_github:
        results.append(github_checks(args.repository, args.stamp))
    print(json.dumps({"passed": sum(results), "of": len(results)}))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
