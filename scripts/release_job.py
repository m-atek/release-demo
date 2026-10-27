"""The Jenkins release job, as release-bot: start runs, follow them, stop them.

history.py starts releases with the request the Slack handler sends,
buildWithParameters as release-bot with its API token, and reset.py stops
the runs a take left behind. Both run on the tools host, where Jenkins
answers on localhost; anywhere else it sits behind Cloudflare Access.
release-bot may read, start and cancel jobs, nothing more.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from common import fail, get_parameter, ssm_client

JENKINS = os.environ.get("RELEASE_DEMO_JENKINS", "http://localhost:8080")
JOB = "release"
BUILD_FIELDS = "number,result,building,displayName,timestamp,duration,actions[parameters[name,value]]"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Jenkins answers a stop or a cancel with a redirect to a page: that is the answer."""

    def redirect_request(self, *args, **kwargs):
        return None


class ReleaseJob:
    def __init__(self, token, base=JENKINS, job=JOB, user="release-bot"):
        self.base = base.rstrip("/")
        self.job = job
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def as_release_bot(cls):
        return cls(get_parameter(ssm_client(), "jenkins/release-bot-api-token"))

    def call(self, method, path, form=None):
        """Returns (status, headers, body). Requests signed with an API token
        need no crumb."""
        data = urllib.parse.urlencode(form).encode() if form is not None else (b"" if method == "POST" else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": self.auth, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            fail(f"Jenkins doesn't answer at {self.base}: {getattr(error, 'reason', error)}. "
                 f"Is the tools host up? scripts/tools.sh status")

    def text(self, path):
        status, _, body = self.call("GET", path)
        if status == 401:
            fail("Jenkins refused release-bot's token (401). The tools host makes a new one at boot: "
                 "scripts/tools.sh down, then up.")
        if status != 200:
            fail(f"Jenkins GET {path}: {status}")
        return body.decode(errors="replace")

    def get(self, path):
        return json.loads(self.text(path))

    def start(self, params):
        """Queue a run with these parameters. Returns the queue item id."""
        status, headers, _ = self.call("POST", f"/job/{self.job}/buildWithParameters?delay=0sec", params)
        location = headers.get("Location", "") if headers else ""
        if status != 201 or "/queue/item/" not in location:
            fail(f"Jenkins didn't queue {self.job} ({status}).")
        return int(location.rstrip("/").rsplit("/", 1)[1])

    def started(self, queue_id, timeout=1800, interval=3, waiting=None):
        """The build number once the queued run starts. waiting(why) is told,
        once, why it waits, for example behind another run."""
        deadline = time.monotonic() + timeout
        told = False
        while time.monotonic() < deadline:
            item = self.get(f"/queue/item/{queue_id}/api/json")
            if item.get("executable"):
                return item["executable"]["number"]
            if item.get("cancelled"):
                fail(f"Queued run {queue_id} was cancelled before it started.")
            if waiting and not told and item.get("why"):
                waiting(item["why"])
                told = True
            time.sleep(interval)
        fail(f"Queued run {queue_id} didn't start within {timeout // 60} minutes.")

    def builds(self, limit=50):
        """The newest runs first, with their parameters as a dict."""
        reply = self.get(f"/job/{self.job}/api/json?tree=builds[{BUILD_FIELDS}]{{0,{limit}}}")
        return [with_parameters(build) for build in reply.get("builds", [])]

    def build(self, number):
        return with_parameters(self.get(f"/job/{self.job}/{number}/api/json?tree={BUILD_FIELDS}"))

    def stages(self, number):
        """[(name, status, seconds)] from Pipeline Stage View, in order; none
        while a run that has just started has no stages yet."""
        status, _, body = self.call("GET", f"/job/{self.job}/{number}/wfapi/describe")
        if status != 200:
            return []
        return [(stage["name"], stage["status"], stage.get("durationMillis", 0) / 1000)
                for stage in json.loads(body).get("stages", [])]

    def console(self, number):
        return self.text(f"/job/{self.job}/{number}/consoleText")

    def queued(self):
        """This job's runs waiting in the queue: [{id, parameters}]."""
        reply = self.get("/queue/api/json?tree=items[id,task[name],actions[parameters[name,value]]]")
        return [with_parameters(item) for item in reply.get("items", []) if item.get("task", {}).get("name") == self.job]

    def stop(self, number):
        status, _, _ = self.call("POST", f"/job/{self.job}/{number}/stop")
        if status >= 400:
            fail(f"Jenkins didn't stop {self.job} #{number} ({status}).")

    def cancel(self, queue_id):
        status, _, _ = self.call("POST", f"/queue/cancelItem?id={queue_id}")
        if status >= 400 and status != 404:
            fail(f"Jenkins didn't cancel queued run {queue_id} ({status}).")


def with_parameters(item):
    """A build or queue item, with "parameters": {name: value} from its actions."""
    parameters = {}
    for action in item.get("actions") or []:
        for parameter in (action or {}).get("parameters") or []:
            parameters[parameter["name"]] = parameter.get("value")
    return {**item, "parameters": parameters}


def version_of(build):
    """The version from the run's name, "#12 orders-service 1.2.0", once the run has set it."""
    words = (build.get("displayName") or "").split()
    return words[2] if len(words) == 3 else None
