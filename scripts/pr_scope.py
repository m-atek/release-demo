"""Does a pull request change anything under a folder? For the multibranch
jobs, which each discover every pull request in the repository and build
only their own: network-stack (demo 2) and catalog-production (demo 19).

    python scripts/pr_scope.py infra/demos/2-terraform           the pull request of this build (CHANGE_ID)
    python scripts/pr_scope.py deploy/production/catalog --pr 7

Exits 0 when it does, 1 when it changes nothing there, and 2 when GitHub
can't say, so a job never skips a pull request it should have built. Reads
the pull request's files through the GitHub App.
"""

import argparse
import os
import sys

from common import github_api, github_app_token, ssm_client

NOT_IN_SCOPE = 1
UNKNOWN = 2


def files(number, ssm=None):
    token, repository, _ = github_app_token(ssm or ssm_client())
    names, page = [], 1
    while True:
        batch = github_api("GET", f"/repos/{repository}/pulls/{number}/files?per_page=100&page={page}", token)
        names += [item["filename"] for item in batch or []]
        if not batch or len(batch) < 100:
            return names
        page += 1


def touches(names, folder):
    folder = folder.rstrip("/") + "/"
    return any(name.startswith(folder) for name in names)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder")
    parser.add_argument("--pr", default=os.environ.get("CHANGE_ID"), help="the pull request; default CHANGE_ID")
    args = parser.parse_args()
    if not args.pr:
        print("No pull request: CHANGE_ID isn't set and --pr isn't given.", file=sys.stderr)
        return UNKNOWN
    try:
        names = files(args.pr)
    except (Exception, SystemExit) as error:  # common.fail() exits 1, which would read as "not ours"
        print(f"Couldn't read pull request #{args.pr}'s files: {error.__class__.__name__}: {error}", file=sys.stderr)
        return UNKNOWN
    if touches(names, args.folder):
        print(f"Pull request #{args.pr} changes {args.folder}/.")
        return 0
    print(f"Not for this pipeline: pull request #{args.pr} changes nothing in {args.folder}/.")
    return NOT_IN_SCOPE


if __name__ == "__main__":
    sys.exit(main())
