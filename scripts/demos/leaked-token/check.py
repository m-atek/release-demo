"""Video 10's acceptance checks 2, 3 and 6, after a take or a rehearsal. One ok or FAIL line each.

    python scripts/demos/leaked-token/check.py            both parts: on the tools host, then on the Mac
    python scripts/demos/leaked-token/check.py --part host
    python scripts/demos/leaked-token/check.py --part records

On the tools host (2 and 3): the leaked token, the newest token of
release-pipeline in #releases that JFrog no longer lists, gets 401 from
JFrog on the host and through Cloudflare Access (dc-agent's service token,
which Access lets through to JFrog); the token in SSM gets 200 both ways;
and SSM, the tmpfs file and Jenkins' credential jcr-token hold the same
token, compared by SHA-256.

On the Mac (6): the rotation's log, the leak incident, the Slack thread and
the Grafana annotations hold no token: neither the leaked one nor the new
one, and gitleaks finds nothing in them. Nothing prints a value.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import take  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402

ACCESS_JFROG = "https://artifactory-demo.es.nl/artifactory/api/repositories"
LOCAL_JFROG = "http://localhost:8082/artifactory/api/repositories"
TMPFS = Path("/run/release-demo/secrets/jcr-token")
# Prints the SHA-256 of the credential's secret, never the secret.
JENKINS_HASH = """
import com.cloudbees.plugins.credentials.CredentialsProvider
import org.jenkinsci.plugins.plaincredentials.StringCredentials
def c = CredentialsProvider.lookupCredentials(StringCredentials, jenkins.model.Jenkins.get(), null, null).find { it.id == 'jcr-token' }
def d = java.security.MessageDigest.getInstance('SHA-256').digest(c.secret.plainText.trim().getBytes('UTF-8'))
println d.collect { String.format('%02x', it) }.join()
"""


def line(ok, text):
    print(f"{'ok  ' if ok else 'FAIL'} {text}", flush=True)
    return ok


def status(url, token, headers=None):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", **(headers or {}),
                                                   "User-Agent": "release-demo leaked-token check"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def leaked_token(ssm, live_ids):
    """The newest token of release-pipeline in #releases that JFrog no longer lists: the one a take leaked."""
    from rotators import jwt_claims, records
    from rotators.jcr import subject_is_user

    messages = records.history(get_parameter(ssm, "slack/bot-token"), get_parameter(ssm, "slack/channel-id"))
    for message in messages:
        for token in records.tokens_in(message.get("text")):
            claims = jwt_claims(token)
            if subject_is_user(claims) and claims.get("jti") not in live_ids:
                return message, token
    return None, None


def jenkins_hash(ssm, client=None):
    """Jenkins refuses a POST to its script console without a crumb (403), so ask
    for one first, in the same session, as rotate.py does for its reload."""
    from jcr_setup import JENKINS, Client, Form

    jenkins = client or Client(JENKINS, "admin", get_parameter(ssm, "jenkins/admin-password"))
    status, crumb = jenkins.call("GET", "/crumbIssuer/api/json")
    if status != 200 or not isinstance(crumb, dict):
        raise RuntimeError(f"Jenkins gave no crumb ({status})")
    status, reply = jenkins.call("POST", "/scriptText", Form(script=JENKINS_HASH),
                                 headers={crumb["crumbRequestField"]: crumb["crumb"]})
    if status != 200:
        raise RuntimeError(f"Jenkins' script console answered {status}")
    return str(reply).strip()


def on_host():
    ssm = ssm_client()
    client = take.admin_client(ssm)
    live = {t["token_id"] for t in take.user_tokens(client)}
    current = get_parameter(ssm, "jcr/token")
    access = {"CF-Access-Client-Id": get_parameter(ssm, "cloudflare/dc-agent-client-id"),
              "CF-Access-Client-Secret": get_parameter(ssm, "cloudflare/dc-agent-client-secret")}
    _, old = leaked_token(ssm, live)
    results = []
    if old is None:
        results.append(line(False, "2. no leaked token of release-pipeline in #releases' last 100 messages"))
    else:
        local, remote = status(LOCAL_JFROG, old), status(ACCESS_JFROG, old, access)
        results.append(line(local == 401 and remote == 401,
                            f"2. the leaked token ({take.describe(old)}): HTTP {local} on the host, {remote} through Access"))
    local, remote = status(LOCAL_JFROG, current), status(ACCESS_JFROG, current, access)
    results.append(line(local == 200 and remote == 200,
                        f"2. the token in SSM ({take.describe(current)}): HTTP {local} on the host, {remote} through Access"))
    digest = lambda text: hashlib.sha256(text.strip().encode()).hexdigest()
    ssm_hash = digest(current)
    tmpfs_hash = digest(TMPFS.read_text()) if TMPFS.exists() else "missing"
    try:
        jenkins = jenkins_hash(ssm)
    except Exception as error:
        jenkins = f"unknown ({error.__class__.__name__})"
    results.append(line(ssm_hash == tmpfs_hash == jenkins,
                        f"3. SSM, the tmpfs and Jenkins' jcr-token hold the same token (SHA-256 {ssm_hash[:12]}...: "
                        f"tmpfs {'same' if tmpfs_hash == ssm_hash else 'differs'}, "
                        f"Jenkins {'same' if jenkins == ssm_hash else 'differs'})"))
    return 0 if all(results) else 1


def records_texts(ssm):
    """The rotation's log, the newest leak incident, its Slack thread and Grafana's secret-rotation annotations."""
    import host
    from change import connect
    from rotators import records

    texts = {}
    _, log, _ = host.Host().shell("tail -c 20000 /var/log/release-demo-rotate.log")
    texts["rotate.log"] = log
    sn, _ = connect()
    incidents = sn.call("GET", "/api/now/table/incident", sysparm_limit=1, sysparm_fields="sys_id,number,description,close_notes",
                        sysparm_query=f"short_descriptionSTARTSWITH{records.INCIDENT_PREFIX}^ORDERBYDESCnumber") or []
    for incident in incidents:
        notes = sn.call("GET", "/api/now/table/sys_journal_field", sysparm_fields="value",
                        sysparm_query=f"element_id={incident['sys_id']}") or []
        texts[f"{incident['number']}.json"] = json.dumps({"incident": incident, "notes": notes})
    token = get_parameter(ssm, "slack/bot-token")
    channel = get_parameter(ssm, "slack/channel-id")
    message, _ = leaked_token(ssm, set())
    if message:
        replies = records.slack_get("conversations.replies", token, channel=channel, ts=message["ts"])
        texts["slack-thread.json"] = json.dumps([m.get("text") for m in replies.get("messages", [])[1:]])
    stack = get_parameter(ssm, "grafana/stack-url").rstrip("/")
    request = urllib.request.Request(f"{stack}/api/annotations?tags=secret-rotation&limit=20", headers={
        "Authorization": f"Bearer {get_parameter(ssm, 'grafana/service-account-token')}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        texts["grafana-annotations.json"] = response.read().decode()
    return texts, message


def records_check():
    ssm = ssm_client()
    texts, message = records_texts(ssm)
    from rotators import records

    secrets = [get_parameter(ssm, "jcr/token")]
    if message:
        secrets += records.tokens_in(message.get("text"))
    joined = "\n".join(texts.values())
    clean = not any(secret in joined or secret.split(".")[1] in joined for secret in secrets)
    results = [line(clean, f"6. no token in {', '.join(texts)} (the leaked one and the new one, searched as values)")]
    with tempfile.TemporaryDirectory(prefix="release-demo-check-") as folder:
        for name, text in texts.items():
            (Path(folder) / name).write_text(text)
        scan = subprocess.run(["gitleaks", "dir", folder, "--no-banner", "--redact"], capture_output=True, text=True)
        results.append(line(scan.returncode == 0, "6. gitleaks finds nothing in them"
                            + ("" if scan.returncode == 0 else f": {scan.stdout.strip()[-200:]}")))
    return 0 if all(results) else 1


def main():
    import host

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--part", choices=["host", "records", "both"], default="both")
    parser.add_argument("--here", action="store_true")
    args = parser.parse_args()
    if args.part == "host" and (args.here or host.on_host()):
        return on_host()
    code = 0
    if args.part in ("host", "both"):
        code |= host.hand_over("leaked-token-check", "demos/leaked-token/check.py", ["--part", "host"],
                               title="check.py leaked-token host")
    if args.part in ("records", "both"):
        code |= records_check()
    return code


if __name__ == "__main__":
    sys.exit(main())
