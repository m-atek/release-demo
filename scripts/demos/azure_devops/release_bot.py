"""Azure DevOps as Release Bot, with no stored secret, and the names demo 22 shares.

    import release_bot
    release_bot.token()                   an Azure DevOps access token as Release Bot
    release_bot.call("GET", "/_apis/...")  a call to the organization as Release Bot
    release_bot.run_in_progress()         a release run still going, for the tools host's idle stop

AWS signs a five-minute token for the caller's role (sts:GetWebIdentityToken,
AWS's outbound identity federation), and Entra, which trusts that role
through one of Release Bot's federated credentials
(infra/demos/22-azure-devops-approval), exchanges it for an Azure DevOps
token. Two roles are trusted: the Slack handler's and the tools host's. So
this works on the tools host, not with your sign-in on the Mac; there, the
scripts hand themselves over to the host, as reset.py does.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

from common import ROOT, SSM_PREFIX, aws_session, fail, get_parameter, put_parameter, ssm_client  # noqa: E402,F401

PROJECT = "release-demo"
POOL = "release-demo"
AGENT = "tools-host"
UNIT = "release-demo-azure-agent"
SWITCH = "ci/orders-service"
SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"  # Azure DevOps, as Entra names it
AUDIENCE = "api://AzureADTokenExchange"
USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


def settings(ssm=None):
    ssm = ssm or ssm_client()
    return {name: get_parameter(ssm, f"azure-devops/{name}")
            for name in ("organization", "pipeline-id", "tenant-id", "release-bot-client-id")}


def token(values=None, session=None):
    values = values or settings()
    session = session or aws_session()
    assertion = session.client("sts").get_web_identity_token(
        Audience=[AUDIENCE], SigningAlgorithm="RS256", DurationSeconds=300)["WebIdentityToken"]
    form = {"client_id": values["release-bot-client-id"], "scope": SCOPE, "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion}
    request = urllib.request.Request(f"https://login.microsoftonline.com/{values['tenant-id']}/oauth2/v2.0/token",
                                     method="POST", data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())["access_token"]
    except urllib.error.HTTPError as error:
        fail(f"Entra didn't sign Release Bot in ({error.code}). On the Mac that is expected: Entra trusts only the "
             "Slack handler's and the tools host's roles.")


def call(method, path, body=None, values=None, bearer=None):
    """A call to the organization, https://dev.azure.com/<organization><path>."""
    values = values or settings()
    bearer = bearer or token(values)
    request = urllib.request.Request(f"https://dev.azure.com/{values['organization']}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw.strip() else None


def runs_in_progress(values=None, bearer=None):
    values = values or settings()
    reply = call("GET", f"/{PROJECT}/_apis/pipelines/{values['pipeline-id']}/runs?api-version=7.1",
                 values=values, bearer=bearer)
    return [run for run in (reply or {}).get("value", []) if run.get("state") == "inProgress"]


def run_in_progress():
    return bool(runs_in_progress())


def main():
    """python scripts/demos/azure_devops/release_bot.py whoami: who Azure DevOps sees, on the tools host."""
    if sys.argv[1:] != ["whoami"]:
        fail("usage: release_bot.py whoami")
    reply = call("GET", "/_apis/connectionData")
    print(f"Azure DevOps sees: {reply['authenticatedUser']['providerDisplayName']}")


if __name__ == "__main__":
    main()
