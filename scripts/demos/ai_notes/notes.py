"""What the AI notes share (videos 12 and 13): the service's ai_notes settings,
the switch, the release build's archive, and the writers.

The scripts here run from the repository root like the shared ones, which
they import from scripts/. Only the writers write, and only to the change (as
the ServiceNow user ai.notes), the release's Slack thread and the Jira issue:
nothing here approves, blocks or changes a release.
"""

import json
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

from common import service, ssm_client  # noqa: E402

NOTES_USER = "ai.notes"
NOTES_PASSWORD = "servicenow/ai-notes-password"
# The files the job takes from the release build's archive, where it finds them.
ARCHIVED = [".release/evidence.json", ".release/check-staging.txt", ".release/check-production.txt",
            ".release/reports/junit.xml", ".release/reports/grype.json", ".release/reports/sbom.cdx.json"]


def settings(service_name):
    """The service's ai_notes in services.yaml, or {} for a service without them."""
    return service(service_name).get("ai_notes") or {}


def fetch_archive(job, build, folder):
    """Copy the release build's archived evidence, reports and check output to folder.
    job is a ReleaseJob for the release job, as release-bot. Returns what it found."""
    found = []
    for name in ARCHIVED:
        status, _, body = job.call("GET", f"/job/{job.job}/{build}/artifact/{urllib.parse.quote(name)}")
        if status == 200:
            target = Path(folder) / name.removeprefix(".release/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            found.append(name)
    return found


def notes_servicenow(ssm=None):
    """ServiceNow as ai.notes: it writes work notes and attaches files, and approves nothing."""
    from common import get_parameter
    from servicenow_setup import ServiceNow

    ssm = ssm or ssm_client()
    return ServiceNow(get_parameter(ssm, "servicenow/instance-url"), NOTES_USER, get_parameter(ssm, NOTES_PASSWORD))


def work_note(sn, change_sys_id, text):
    sn.call("PATCH", f"/api/now/table/change_request/{change_sys_id}", {"work_notes": text})


def attach(sn, change_sys_id, path):
    from change import attach as attach_file

    attach_file(sn, change_sys_id, Path(path))


def slack_thread_reply(channel, ts, text, ssm=None):
    """A reply in the release message's thread, links not unfurled. Nothing goes to the channel itself."""
    from common import get_parameter
    from notify import slack

    token = get_parameter(ssm or ssm_client(), "slack/bot-token")
    reply = slack("chat.postMessage", token, channel=channel, thread_ts=ts, text=text,
                  unfurl_links=False, unfurl_media=False)
    if not reply.get("ok"):
        raise RuntimeError(f"Slack refused the thread reply: {reply.get('error')}")
    return reply.get("ts")


def jira_comment(key, text):
    from jira import Jira, paragraph

    Jira().call("POST", f"/rest/api/3/issue/{key}/comment", {"body": paragraph(text)})


def send_event(body, ssm=None):
    """One event to Splunk and Grafana Logs, as events.py sends the pipeline's."""
    from events import to_loki, to_splunk

    ssm = ssm or ssm_client()
    return to_splunk(ssm, body), to_loki(ssm, body)


def each(writers):
    """Run every writer; one that fails says so and doesn't stop the others.
    Returns the names of the writers that failed."""
    failed = []
    for name, write in writers:
        try:
            write()
            print(f"  {name}: done")
        except BaseException as error:  # fail() exits: a writer still mustn't stop the others
            failed.append(name)
            print(f"  {name}: failed ({error.__class__.__name__}: {str(error)[:200]})")
    return failed


def load_json(path):
    return json.loads(Path(path).read_text())


def plain(text):
    """Slack's text without its markup, for the change and Jira: the same words."""
    return text.replace("*", "").replace("`", "").replace("• ", "- ")


