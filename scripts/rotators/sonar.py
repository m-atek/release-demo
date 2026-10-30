"""The SonarQube rotator: the quality gate's SonarQube Cloud token.

    scripts/rotate.py sonar --reason "yearly" --old-name "<the token you made by hand>"   the first time
    scripts/rotate.py sonar --reason "yearly"                                              after that

Through SonarQube Cloud's user token API: generate release-demo-<UTC time>,
store it in SSM sonar/token, reload Jenkins (the quality gate reads the
credential sonar-token), revoke the old token by name, and prove it dead
with api/authentication/validate ("valid": false). The old names are
--old-name and every earlier release-demo-* token of the same user. Not in
video 10: it makes the inventory's "by script" count true for two secrets.
"""

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

from rotators import Failed

SECRET = "sonar/token"
API = "https://sonarcloud.io/api"
PREFIX = "release-demo-"
CREDENTIAL_ID = "sonar-token"


def call(method, path, token, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(f"{API}/{path}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, None


class Sonar:
    def generate(self, token, name):
        status, reply = call("POST", "user_tokens/generate", token, {"name": name})
        if status != 200 or not (reply or {}).get("token"):
            raise Failed(f"SonarQube Cloud didn't generate {name} ({status})")
        return reply["token"]

    def names(self, token):
        status, reply = call("GET", "user_tokens/search", token)
        if status != 200:
            raise Failed(f"SonarQube Cloud didn't list the tokens ({status})")
        return [t["name"] for t in (reply or {}).get("userTokens", [])]

    def revoke(self, token, name):
        status, _ = call("POST", "user_tokens/revoke", token, {"name": name})
        if status not in (200, 204):
            raise Failed(f"SonarQube Cloud didn't revoke {name} ({status})")

    def valid(self, token):
        status, reply = call("GET", "authentication/validate", token)
        return bool(status == 200 and (reply or {}).get("valid"))


def token_name(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"{PREFIX}{now:%Y%m%dT%H%M%SZ}"


def run(steps, reason, old_name=None, host=None, sonar=None):
    from rotators.jcr import Host

    host = host or Host()
    sonar = sonar or Sonar()
    old = host.get(SECRET)
    name = token_name()
    new = sonar.generate(old, name)
    steps.line("issued", name)
    steps.record(f"New token issued ({name})")
    host.put(SECRET, new)
    steps.line("stored", f"SSM /release-demo/{SECRET}")
    host.refresh_jenkins()
    steps.line("reloaded", f"Jenkins credential {CREDENTIAL_ID}")
    old_names = [n for n in sonar.names(new) if n != name and (n.startswith(PREFIX) or n == old_name)]
    if old_name and old_name not in old_names:
        raise Failed(f"SonarQube Cloud has no token named {old_name}: the old one stays valid")
    if not old_names:
        raise Failed("No old token to revoke: give the first one's name with --old-name")
    for each in old_names:
        sonar.revoke(new, each)
        steps.line("revoked", each)
    dead, alive = not sonar.valid(old), sonar.valid(new)
    steps.line("old token", f"valid: {str(not dead).lower()} at SonarQube")
    steps.line("new token", f"valid: {str(alive).lower()} at SonarQube")
    if not dead:
        raise Failed("SonarQube Cloud still accepts the old token")
    if not alive:
        raise Failed("SonarQube Cloud doesn't accept the new token")
    host.event({"result": "ok", "secret": SECRET, "new_token_name": name, "revoked": old_names, "reason": reason})
    steps.last(f"Rotated: the old token is dead, {name} is live.")
    return 0
