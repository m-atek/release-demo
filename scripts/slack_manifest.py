"""Print the Slack app's manifest with the Slack handler's URL filled in.

    python scripts/slack_manifest.py                 the URL from Terraform: scripts/tf.sh output
    python scripts/slack_manifest.py --url https://...lambda-url.eu-west-1.on.aws/

Paste the output into the app's settings: api.slack.com/apps, Release Bot,
App Manifest. slack/manifest.yml keeps a placeholder, because the repository
is public and the URL is the account's own. Nothing is written to a file.
"""

import argparse
import re
import subprocess
import sys

from common import ROOT, fail

PLACEHOLDER = "https://REPLACE-WITH-SLACK-HANDLER-URL.invalid/"


def handler_url():
    result = subprocess.run([str(ROOT / "scripts" / "tf.sh"), "output", "-raw", "slack_handler_url"],
                            capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.startswith("https://"):
        fail("No slack_handler_url in Terraform's outputs yet. Apply infra/ first: scripts/tf.sh apply")
    return result.stdout.strip()


def manifest(url):
    if not re.fullmatch(r"https://[a-z0-9]+\.lambda-url\.[a-z0-9-]+\.on\.aws/?", url):
        fail(f"{url} doesn't look like a Lambda function URL (https://<id>.lambda-url.<region>.on.aws/).")
    text = (ROOT / "slack" / "manifest.yml").read_text()
    if PLACEHOLDER not in text:
        fail(f"slack/manifest.yml has no {PLACEHOLDER} to replace.")
    return text.replace(PLACEHOLDER, url.rstrip("/") + "/")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="the handler's function URL; default: Terraform's slack_handler_url")
    args = parser.parse_args()
    print(manifest(args.url or handler_url()), end="")


if __name__ == "__main__":
    sys.exit(main())
