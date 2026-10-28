"""The pull request the video uses: "ORD-22 Store exports bucket and queue".

    python scripts/demos/terraform/demo_pr.py open    the first commit: exports.tf with a public bucket, and the pull request
    python scripts/demos/terraform/demo_pr.py fix     the second commit: the bucket made private

scripts/demo.sh open-pr terraform and fix-pr terraform run these. Both
commits come from demo-states/network-stack/ and go to the branch
demo-2/store-exports through the GitHub App, so no clone or token on the Mac
is needed. The commits carry the repository owner's GitHub identity, which
people.yaml maps to Sam, who asks for the change. reset.py terraform puts it
all back.
"""

import argparse
import sys

import stack
import github
from common import fail

STATES = stack.ROOT / "demo-states" / stack.SERVICE
BODY = """Store exports for ORD-22: each store's nightly export lands in an S3 bucket, and S3 puts a message on an SQS queue, so the reporting job picks it up.

Jenkins runs the checks on every push, posts the plan and its monthly cost here, and opens a change in ServiceNow. After approval it applies exactly that plan and merges this pull request."""


def open_pr(gh, config):
    branch, path = config["demo_branch"], config["demo_file"]
    main = gh.branch_sha("main")
    if gh.file_at(path, main):
        fail(f"main already has {path}: run python scripts/demos/terraform/reset.py first")
    for pull in gh.open_pulls(branch):
        fail(f"Pull request #{pull['number']} is already open: {pull['html_url']}. "
             "Push the fix with fix-pr, or start again with reset.py.")
    commit = gh.commit_files(main, {path: (STATES / "exports-public" / "exports.tf").read_text()},
                             "ORD-22 Add the store exports bucket and queue", author=gh.owner_identity())
    gh.set_branch(branch, commit, force=True)
    print(f"Pushed {commit[:7]} to {branch}: exports.tf, with a public bucket")
    pull = gh.create_pull(config["demo_title"], branch, BODY)
    print(f"Opened pull request #{pull['number']}: {pull['html_url']}")
    print("Jenkins finds it within a minute. Checkov and the house rule \"S3 bucket must not be public\" fail.")


def fix_pr(gh, config):
    branch, path = config["demo_branch"], config["demo_file"]
    head = gh.branch_sha(branch)
    if not head or not gh.open_pulls(branch):
        fail(f"No open pull request from {branch}: open it first with open-pr")
    fixed = (STATES / "exports-private" / "exports.tf").read_text()
    if gh.file_at(path, head) == fixed:
        fail(f"{branch} already has the private bucket")
    commit = gh.commit_files(head, {path: fixed}, "ORD-22 Keep the exports bucket private",
                             author=gh.owner_identity())
    gh.set_branch(branch, commit)
    print(f"Pushed {commit[:7]} to {branch}: the bucket blocks public access, and the public policy is gone")
    print("The checks pass, then the plan and the cost difference appear on the pull request.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["open", "fix"])
    args = parser.parse_args()
    config = stack.settings()
    gh = github.GitHub()
    open_pr(gh, config) if args.step == "open" else fix_pr(gh, config)


if __name__ == "__main__":
    sys.exit(main())
