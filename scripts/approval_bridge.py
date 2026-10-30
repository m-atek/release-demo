"""ServiceNow's side of the approval bridge (bridge/handler.py), for servicenow_setup.py.

A business rule, "release-demo: approval bridge", calls the bridge when the
approval of a change opened from GitHub Actions turns approved or rejected,
so the waiting run gets its answer within seconds of Alex's click. It reads
two system properties: the bridge's URL, and the token the bridge checks,
which is kept in SSM too (bridge/servicenow-token) and generated here the
first time. The call carries only the change's sys_id: the bridge reads the
change itself. Idempotent, like the rest of the setup.
"""

import secrets

from common import SSM_PREFIX, put_parameter

RULE = "release-demo: approval bridge"
URL_PROPERTY = "release_demo.approval_bridge.url"
TOKEN_PROPERTY = "release_demo.approval_bridge.token"
# The correlation ids change.py writes for runs that wait on the bridge.
PREFIXES = ["github-actions:"]

SCRIPT = """(function executeRule(current, previous) {
    // Tells the approval bridge that this change's approval is decided. The
    // bridge reads the change itself, as release.pipeline: the body only names it.
    var url = gs.getProperty('%(url)s', '');
    var token = gs.getProperty('%(token)s', '');
    if (!url || !token) {
        gs.warn('%(rule)s: the bridge URL or token property is empty');
        return;
    }
    var request = new sn_ws.RESTMessageV2();
    request.setEndpoint(url.replace(/\\/+$/, '') + '/servicenow');
    request.setHttpMethod('POST');
    request.setRequestHeader('Authorization', 'Bearer ' + token);
    request.setRequestHeader('Content-Type', 'application/json');
    request.setRequestBody(JSON.stringify({sys_id: current.getUniqueValue()}));
    request.setHttpTimeout(5000);
    try {
        var response = request.execute();
        if (response.getStatusCode() >= 300) {
            gs.warn('%(rule)s: the bridge answered ' + response.getStatusCode() + ' for ' + current.getValue('number'));
        }
    } catch (error) {
        gs.warn('%(rule)s: the bridge was not reached for ' + current.getValue('number') + ': ' + error);
    }
})(current, previous);
""" % {"url": URL_PROPERTY, "token": TOKEN_PROPERTY, "rule": RULE}


def condition(prefixes=None):
    """Only when the approval changes to approved or rejected, and only for changes of runs that wait on the bridge."""
    runs = " || ".join(f"current.correlation_id.toString().indexOf('{prefix}') == 0" for prefix in prefixes or PREFIXES)
    return ("current.approval.changes() && (current.approval == 'approved' || current.approval == 'rejected') "
            f"&& ({runs})")


def bridge_url(ssm):
    """The bridge's function URL, which infra/approval_bridge.tf writes to SSM, or None before it is applied."""
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + "bridge/url")["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    return None if value == "unset" else value.rstrip("/")


def bridge_token(ssm):
    """The token ServiceNow sends and the bridge checks, generated once into SSM."""
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + "bridge/servicenow-token", WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        value = None
    if value in (None, "unset"):
        value = secrets.token_urlsafe(32)
        put_parameter(ssm, "bridge/servicenow-token", value)
        print("  bridge/servicenow-token: generated and stored in SSM")
    return value


def setup(sn, ssm):
    url = bridge_url(ssm)
    if not url:
        print("  skipped: the approval bridge isn't applied yet (no bridge/url in SSM). Apply infra/, then run this again.")
        return
    sn.ensure("sys_properties", f"name={URL_PROPERTY}", {
        "name": URL_PROPERTY, "type": "string", "value": url,
        "description": "The release-demo approval bridge's function URL. Set by scripts/servicenow_setup.py."},
        f"property {URL_PROPERTY}")
    # An encrypted property: its value never reads back, so it is set on every run.
    token = bridge_token(ssm)
    prop = sn.find("sys_properties", f"name={TOKEN_PROPERTY}", "sys_id")
    values = {"name": TOKEN_PROPERTY, "type": "password2", "value": token,
              "description": "The token the release-demo approval bridge checks. Kept in SSM as bridge/servicenow-token."}
    if prop:
        sn.call("PATCH", f"/api/now/table/sys_properties/{prop['sys_id']}", values)
    else:
        sn.call("POST", "/api/now/table/sys_properties", values)
    print(f"  property {TOKEN_PROPERTY}: set, encrypted")
    sn.ensure("sys_script", f"name={RULE}^collection=change_request", {
        "name": RULE, "collection": "change_request", "when": "after", "order": "100", "active": "true",
        "advanced": "true", "action_insert": "false", "action_update": "true", "action_delete": "false",
        "action_query": "false", "condition": condition(), "script": SCRIPT,
        "description": "Tells the release-demo approval bridge when the approval of a change opened by a "
                       "GitHub Actions run is decided, so the waiting run gets its answer at once."},
        f"business rule '{RULE}'")
