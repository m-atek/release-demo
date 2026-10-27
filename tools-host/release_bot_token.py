"""Make sure SSM holds a working Jenkins API token for release-bot.

    release_bot_token.py /run/release-demo/secrets

The Slack handler starts releases as release-bot with this token. If the
stored token still signs in, nothing happens; otherwise a new one is made,
signed in with release-bot's password, and stored. Run by boot.sh.
"""

import base64
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import SSM_PREFIX, aws_session, put_parameter  # noqa: E402

JENKINS = "http://localhost:8080"
NAME = "jenkins/release-bot-api-token"


def basic(user, secret):
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def signs_in(token):
    request = urllib.request.Request(JENKINS + "/whoAmI/api/json", headers={"Authorization": basic("release-bot", token)})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read()).get("name") == "release-bot"
    except urllib.error.HTTPError:
        return False


def main():
    secrets = Path(sys.argv[1])
    ssm = aws_session().client("ssm")
    current = ssm.get_parameter(Name=SSM_PREFIX + NAME, WithDecryption=True)["Parameter"]["Value"]
    if current != "unset" and signs_in(current):
        print("release-bot token: stored token works")
        return

    password = (secrets / "jenkins-release-bot-password").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    auth = {"Authorization": basic("release-bot", password)}
    with opener.open(urllib.request.Request(JENKINS + "/crumbIssuer/api/json", headers=auth), timeout=10) as response:
        crumb = json.loads(response.read())
    request = urllib.request.Request(
        JENKINS + "/me/descriptorByName/jenkins.security.ApiTokenProperty/generateNewToken", method="POST",
        data=urllib.parse.urlencode({"newTokenName": "slack-handler"}).encode(),
        headers={**auth, crumb["crumbRequestField"]: crumb["crumb"]})
    with opener.open(request, timeout=10) as response:
        token = json.loads(response.read())["data"]["tokenValue"]
    put_parameter(ssm, NAME, token)
    print("release-bot token: created and stored")


if __name__ == "__main__":
    main()
