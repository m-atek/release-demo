"""Tag the released commit and publish its release notes on GitHub.

    python scripts/tag.py                     tag <service>-v<version> on the run's commit, with a GitHub release
    python scripts/tag.py --delete orders-service-v1.4.0      for reset.py

Signs in as the GitHub App, which may write repository contents.
"""

import argparse
import sys
import urllib.error

import run_state
from common import github_api, github_app_token, ssm_client


def notes(state):
    change = state.get("change", {})
    lines = [
        f"{state['service']} {state['version']} for {state['jira_key']}.",
        "",
        f"- Change: {change.get('number', 'none')} ({change.get('type', '')})",
        f"- Package: `{state['package']['path']}`",
        f"- SHA-256: `{state['package']['sha256']}`",
        f"- Total time: {state.get('total', 'n/a')}",
        "",
        "Released by the release-demo pipeline. Demo system, not a client's.",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delete", metavar="TAG")
    args = parser.parse_args()

    token, repository, _ = github_app_token(ssm_client())
    call = lambda method, path, body=None: github_api(method, path, token, body, scheme="token")

    if args.delete:
        try:
            release = call("GET", f"/repos/{repository}/releases/tags/{args.delete}")
            call("DELETE", f"/repos/{repository}/releases/{release['id']}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        try:
            call("DELETE", f"/repos/{repository}/git/refs/tags/{args.delete}")
        except urllib.error.HTTPError as error:
            if error.code not in (404, 422):
                raise
        print(f"{args.delete}: tag and release removed")
        return

    state = run_state.load()
    tag = f"{state['service']}-v{state['version']}"
    try:
        call("GET", f"/repos/{repository}/releases/tags/{tag}")
        print(f"{tag}: already released")
        return
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    call("POST", f"/repos/{repository}/releases", {
        "tag_name": tag, "target_commitish": state["commit"], "name": f"{state['service']} {state['version']}",
        "body": notes(state), "draft": False, "prerelease": False})
    print(f"{tag}: tagged {state['commit'][:7]} with release notes")


if __name__ == "__main__":
    sys.exit(main())
