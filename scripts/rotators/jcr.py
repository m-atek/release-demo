"""The JFrog rotator: the release pipeline's JFrog token, revoked first.

    scripts/rotate.py jcr --reason "pasted in Slack"

On the tools host, in this order:

1. Find. Every JFrog token of the user release-pipeline that #releases'
   last 100 messages hold, the one in SSM (jcr/token) or another of the
   same user still live in JFrog, such as the one a take of video 10 pastes
   (clipboard.py). The newest message that holds one gives the leak time,
   its author and the thread to reply in. With none, the leak time is the
   command's start, and the incident says so. It reads nothing else.
2. Revoke, before anything else: every token JFrog lists for
   release-pipeline, the leaked one first, through the token API's revoke
   as admin; where that endpoint is gone, DELETE /access/api/v1/tokens/me
   with the token itself. A release in flight then fails at Publish rather
   than keep a leaked token alive.
3. Incident: opened in ServiceNow as release.pipeline.
4. Issue a new token for release-pipeline, as jcr_setup.py does: same
   scope, valid a year.
5. Store it in SSM jcr/token; host_secrets.py rewrites the tmpfs and
   Jenkins reloads its configuration, so the credential jcr-token holds it
   too. publish.py and the deploy scripts read SSM on every run.
6. Prove: the old token, every 0.5 seconds until JFrog answers 401 (at most
   120 seconds; past that, FAIL with the incident left open), then the new
   one once, which must answer 200. The revoke answers 200 even for a token
   already revoked, so only this call proves anything.
7. Record: the incident's timeline and resolution, a reply in the leak's
   Slack thread, one Splunk event, one Grafana annotation.
"""


import inventory
from rotators import Failed, clock, jwt_claims, leak_to_useless, records, short_id

SECRET = "jcr/token"
USER = "release-pipeline"
CREDENTIAL_ID = "jcr-token"
PROOF_PATH = "/artifactory/api/repositories"  # anonymous access is off: only a live token lists them
POLL_SECONDS = 0.5
PROOF_LIMIT = 120


def subject_is_user(claims):
    return bool(claims) and str(claims.get("sub", "")).endswith(f"/users/{USER}")


class JFrog:
    """JFrog Container Registry on the tools host: admin calls and token probes."""

    def __init__(self, password):
        from jcr_setup import JCR, Client

        self.client = Client(JCR, "admin", password)

    def user_tokens(self):
        status, reply = self.client.call("GET", "/artifactory/api/security/token")
        if status != 200 or not isinstance(reply, dict):
            raise Failed(f"JFrog didn't list its tokens ({status})")
        return [t for t in reply.get("tokens", []) if str(t.get("subject", "")).endswith(f"/users/{USER}")]

    def revoke(self, token_id, value=None):
        from jcr_setup import Form

        status, _ = self.client.call("POST", "/artifactory/api/security/token/revoke", Form(token_id=token_id))
        if status in (404, 405) and value:
            status, _ = self.client.call("DELETE", "/access/api/v1/tokens/me", auth=f"Bearer {value}")
        return status

    def issue(self):
        import jcr_setup

        token = jcr_setup.create_token(self.client)
        if not token:
            raise Failed(f"JFrog has no user {USER}")
        return token

    def answer(self, token):
        """The status JFrog gives this token, as JFrog returns it."""
        return self.client.call("GET", PROOF_PATH, auth=f"Bearer {token}")[0]


class Host:
    """What the rotation touches on the tools host, behind one object so tests can stand in."""

    def __init__(self):
        from change import connect
        from common import people, ssm_client

        self.ssm = ssm_client()
        self.sn, _ = connect()
        self.people = people
        self.jfrog = JFrog(self.get("jcr/admin-password"))

    def get(self, name):
        from common import get_parameter

        return get_parameter(self.ssm, name)

    def put(self, name, value):
        from common import put_parameter

        put_parameter(self.ssm, name, value)

    def slack_history(self):
        return records.history(self.get("slack/bot-token"), self.get("slack/channel-id"))

    def reply(self, ts, text):
        return records.reply_in_thread(self.get("slack/bot-token"), self.get("slack/channel-id"), ts, text)

    def everyone(self):
        return self.people(self.ssm)

    def refresh_jenkins(self):
        import rotate

        return rotate.refresh_jenkins(self.ssm)

    def event(self, body):
        return records.event(self.ssm, body)

    def annotate(self, text):
        return records.annotate(self.ssm, text)


def find(messages, current, live_ids):
    """The newest message holding a live token of release-pipeline, or the one in SSM:
    (message, token, claims), or None. Earlier takes' messages hold dead tokens: skipped."""
    for message in messages:
        for token in records.tokens_in(message.get("text")):
            claims = jwt_claims(token)
            if token == current or (subject_is_user(claims) and claims.get("jti") in live_ids):
                return message, token, claims or {}
    return None


def run(steps, reason, host=None, **_):
    host = host or Host()
    current = host.get(SECRET)
    live = host.jfrog.user_tokens()
    live_ids = {t["token_id"] for t in live}

    # 1. Find
    found = find(host.slack_history(), current, live_ids)
    if found:
        message, leaked, claims = found
        leak_at = float(message["ts"])
        steps.line("found", f"in #releases, posted {clock(leak_at)}")
        who = records.author(message.get("user"), host.everyone())
        steps.record(f"Token posted in #releases by {who}", leak_at)
    else:
        message, leaked, claims = None, current, jwt_claims(current) or {}
        leak_at = steps.started
        steps.line("found", "in no message: from now")
        steps.record("No message in #releases holds the token: the leak time is when rotate.py started", leak_at)
    leaked_id = claims.get("jti") or next(iter(live_ids), "unknown")
    current_id = (jwt_claims(current) or {}).get("jti")

    # 2. Revoke, the leaked one first
    order = [leaked_id] + sorted(i for i in live_ids if i != leaked_id)
    others = 0
    for token_id in order:
        value = leaked if token_id == leaked_id else current if token_id == current_id else None
        status = host.jfrog.revoke(token_id, value)
        if status not in (200, 201, 204):
            raise Failed(f"JFrog didn't revoke {short_id(token_id)} ({status})")
        if token_id == leaked_id:
            revoked_at = steps.line("revoked", f"old token {short_id(leaked_id)}")
        else:
            others += 1
    if others:
        steps.line("revoked", f"{others} more of {USER}")
    steps.record(f"Old token revoked in JFrog (id {short_id(leaked_id)})"
                 + (f", and {others} more of {USER}" if others else ""), revoked_at)

    # 3. Incident
    posted = (f"posted in #releases at {clock(leak_at)} by {records.author(message.get('user'), host.everyone())}"
              if message else "found in no message in #releases; the leak time is when rotate.py started")
    incident = records.open_incident(host.sn, reason, (
        f"The JFrog token of the user {USER}, which the release pipeline publishes and deploys with, was {posted}. "
        f"scripts/rotate.py revoked every token of {USER} at {clock(revoked_at)}, the leaked one first, "
        "and replaces it now. The timeline follows in the work notes."))
    steps.line("incident", f"{incident['number']} opened")

    try:
        # 4. Issue
        new = host.jfrog.issue()
        new_id = (jwt_claims(new) or {}).get("jti", "unknown")
        steps.record(f"New token issued (id {short_id(new_id)})", steps.line("issued", f"new token {short_id(new_id)}"))

        # 5. Store
        host.put(SECRET, new)
        steps.line("stored", f"SSM /release-demo/{SECRET}")
        host.refresh_jenkins()
        steps.record("New token in SSM; Jenkins reloaded", steps.line("reloaded", f"Jenkins credential {CREDENTIAL_ID}"))

        # 6. Prove
        rejected_at, status = None, None
        while steps.now() - revoked_at <= PROOF_LIMIT:
            status = host.jfrog.answer(leaked)
            if status == 401:
                rejected_at = steps.now()
                break
            steps.sleep(POLL_SECONDS)
        if rejected_at is None:
            steps.line("old token", f"HTTP {status} after {PROOF_LIMIT} s: FAIL")
            steps.record(f"Old token still answered HTTP {status} {PROOF_LIMIT} seconds after the revoke")
            raise Failed(f"JFrog still accepts the old token {PROOF_LIMIT} seconds after the revoke")
        steps.line("old token", f"HTTP {status} from JFrog", rejected_at)
        steps.record(f"Old token rejected by JFrog: HTTP {status}", rejected_at)
        accepted = host.jfrog.answer(new)
        steps.line("new token", f"HTTP {accepted} from JFrog")
        steps.record(f"New token accepted: HTTP {accepted}")
        if accepted != 200:
            raise Failed(f"JFrog answers the new token with HTTP {accepted}, not 200")
    except Failed as error:
        records.note(host.sn, incident, steps.timeline_text() + f"\n\nStopped: {error}. The incident stays open.")
        host.event({"result": "failed", "secret": SECRET, "incident": incident["number"]})
        raise

    # 7. Record
    useless = leak_to_useless(rejected_at - leak_at)
    users = inventory.used_by(SECRET)
    records.note(host.sn, incident, steps.timeline_text() + "\n\n"
                 + f"Who uses the token: {inventory.used_by_text(users)}, through SSM on every run, and Jenkins' "
                   f"credential {CREDENTIAL_ID}.\nThe Slack message stays; the token in it no longer works.")
    records.resolve(host.sn, incident, f"Leak to useless: {useless}. Revoked, replaced and proven dead by scripts/rotate.py.")
    steps.line("resolved", incident["number"])
    if message:
        host.reply(message["ts"], f"Revoked at {clock(revoked_at)} and replaced. JFrog rejects the old token "
                                  f"(HTTP 401) since {clock(rejected_at)}. Incident {incident['number']}.")
    host.event({"result": "ok", "secret": SECRET, "incident": incident["number"],
                "leak_to_useless_seconds": int(rejected_at - leak_at), "old_token_id": short_id(leaked_id),
                "new_token_id": short_id(new_id), "reason": reason})
    host.annotate(f"JFrog token of {USER} rotated: leak to useless {useless} ({incident['number']})")
    steps.last(f"Leak to useless: {useless}")
    return 0
