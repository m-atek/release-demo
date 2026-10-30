"""What video 16's scripts share: the coding agent's GitHub App, its token,
its settings in services.yaml, and the people.yaml entry that marks it.

The coding agent has its own GitHub App, release-demo-coding-agent-<owner>:
contents and pull requests write, metadata read, and nothing else; in no
ruleset's bypass list. Its installation tokens are asked for with only those
two rights, on release-demo only, and last an hour at most.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

import yaml  # noqa: E402

from common import ROOT, fail, get_parameter, github_api, service  # noqa: E402

PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
TOKEN_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


def app_name(owner):
    return f"release-demo-coding-agent-{owner}"[:34]


def everyone():
    return yaml.safe_load((ROOT / "people.yaml").read_text())


def agents():
    """The people.yaml entries with agent: true: they write code, and never ask for, review or approve a release."""
    return {key: person for key, person in everyone().items() if person.get("agent")}


def person_for(login, people=None):
    """The people.yaml entry for a GitHub login. A login two people share (yours,
    which plays Sam on GitHub) maps to the releaser."""
    people = people or everyone()
    matches = [(key, p) for key, p in people.items() if p.get("github_login") == login]
    matches.sort(key=lambda item: not item[1].get("releaser"))
    return matches[0] if matches else (None, None)


def settings(name):
    config = service(name)
    if not config.get("coding_agent"):
        fail(f"{name} has no coding_agent in services.yaml: the coding agent doesn't work on it.")
    return config["coding_agent"]


def jwt(app_id, key):
    """The app's JWT, signed with openssl; the key reaches it through a pipe, never the disk."""
    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    now = int(time.time())
    signing_input = encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + encode(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    read_end, write_end = os.pipe()
    os.write(write_end, key.encode())
    os.close(write_end)
    try:
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_end}"],
                                   input=signing_input, capture_output=True, pass_fds=(read_end,), check=True).stdout
    finally:
        os.close(read_end)
    return (signing_input + b"." + encode(signature)).decode()


def app_jwt(ssm):
    return jwt(get_parameter(ssm, "github/agent-app-id"), get_parameter(ssm, "github/agent-app-private-key"))


def installation(ssm, repository=None):
    repository = repository or get_parameter(ssm, "github/repository")
    return github_api("GET", f"/repos/{repository}/installation", app_jwt(ssm))


def token(ssm):
    """An installation token on release-demo only, with contents and pull requests write only."""
    repository = get_parameter(ssm, "github/repository")
    found = installation(ssm, repository)
    reply = github_api("POST", f"/app/installations/{found['id']}/access_tokens", app_jwt(ssm),
                       {"repositories": [repository.split("/")[1]], "permissions": TOKEN_PERMISSIONS})
    return reply["token"], reply["expires_at"]


def bot_identity(slug, fetch=None):
    """The bot's git author: "<slug>[bot]" and its noreply address, from GitHub's public user record."""
    import urllib.request

    login = f"{slug}[bot]"
    url = f"https://api.github.com/users/{urllib.parse.quote(login)}"
    if fetch is None:
        with urllib.request.urlopen(url, timeout=30) as response:
            user = json.loads(response.read())
    else:
        user = fetch(url)
    return login, f"{user['id']}+{login}@users.noreply.github.com"
