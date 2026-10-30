"""What video 10's scripts share: JFrog on the tools host, the tokens of the
user release-pipeline, and the leak incidents in ServiceNow.

The take's token (REVIEW.md decision 16): the take leaks a second, real
token of release-pipeline, made for the take and valid for a day, so the
pipeline's own token never goes into Slack. rotate.py jcr revokes every
token of the user, this one first, and gives the pipeline a new one.
"""

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

from rotators import jwt_claims, short_id  # noqa: E402

USER = "release-pipeline"
TAKE_SECONDS = 24 * 3600


def describe(token, now=None):
    """A token's id and age, never the token: "id 7f3a2c1e, 3 days old"."""
    claims = jwt_claims(token) or {}
    age = (now or time.time()) - claims.get("iat", now or time.time())
    days = int(age // 86400)
    hours = int(age // 3600)
    when = f"{days} days old" if days >= 2 else f"{hours} hours old" if hours >= 1 else "new"
    return f"id {short_id(claims.get('jti'))}, {when}"


def admin_client(ssm):
    from common import get_parameter
    from jcr_setup import JCR, Client

    return Client(JCR, "admin", get_parameter(ssm, "jcr/admin-password"))


def create(client, seconds=TAKE_SECONDS):
    """A token of release-pipeline with the pipeline's own scope: read, annotate and deploy, no delete."""
    from jcr_setup import SCOPE, Form

    status, reply = client.call("POST", "/artifactory/api/security/token",
                                Form(username=USER, scope=SCOPE, expires_in=str(seconds)))
    if status != 200 or not isinstance(reply, dict) or not reply.get("access_token"):
        raise SystemExit(f"JFrog didn't make the take's token ({status})")
    return reply["access_token"]


def user_tokens(client):
    status, reply = client.call("GET", "/artifactory/api/security/token")
    if status != 200 or not isinstance(reply, dict):
        raise SystemExit(f"JFrog didn't list its tokens ({status})")
    return [t for t in reply.get("tokens", []) if str(t.get("subject", "")).endswith(f"/users/{USER}")]


def revoke(client, token_id):
    from jcr_setup import Form

    return client.call("POST", "/artifactory/api/security/token/revoke", Form(token_id=token_id))[0]
