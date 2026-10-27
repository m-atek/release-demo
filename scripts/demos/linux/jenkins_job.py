"""Start the Jenkins job inventory-dc-wave on dc-agent and wait for it.

The tools host can't reach the data-center servers; dc-agent can, and its
outbound connection to Jenkins is the only link between the two. So the
data-center part of a deploy, check or rollback runs as this job on dc-agent,
started from the tools host through Jenkins on localhost as release-bot. Its
console output is copied into the calling build's log, and its results come
back as the job's artifact. Splunk, Slack and the change hear about them from
the tools host, which hands them on.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import fail, get_parameter, ssm_client  # noqa: E402

JENKINS = os.environ.get("JENKINS_LOCAL_URL", "http://localhost:8080")
JOB = "inventory-dc-wave"
NODE = "dc-agent"
RESULTS = ".release/dc-results.jsonl"


class Jenkins:
    def __init__(self, base=JENKINS, user="release-bot", token=None):
        token = token or get_parameter(ssm_client(), "jenkins/release-bot-api-token")
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()

    def call(self, method, path, data=None):
        """Returns (status, headers, body). Never raises on HTTP errors."""
        url = path if path.startswith("http") else self.base + path
        request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            fail(f"Jenkins doesn't answer at {self.base}: {getattr(error, 'reason', error)}")

    def json(self, path):
        status, _, body = self.call("GET", path)
        return json.loads(body) if status == 200 else None


def agent_online(jenkins):
    node = jenkins.json(f"/computer/{NODE}/api/json?tree=offline,offlineCauseReason")
    return node is not None and not node["offline"]


def preflight(jenkins=None):
    """Fails with what to do when dc-agent isn't connected, before any server is touched."""
    jenkins = jenkins or Jenkins()
    if not agent_online(jenkins):
        fail(f"{NODE} isn't connected to Jenkins, so the data-center servers can't be reached. "
             "On the Mac: scripts/demo.sh up linux. Nothing was deployed.")


def local_path(url):
    """The path of an absolute Jenkins URL, so calls stay on localhost."""
    return urllib.parse.urlsplit(url).path


def start(jenkins, request):
    data = urllib.parse.urlencode({"REQUEST": json.dumps(request)}).encode()
    status, headers, body = jenkins.call("POST", f"/job/{JOB}/buildWithParameters", data)
    if status not in (200, 201):
        fail(f"Jenkins didn't start {JOB} ({status}): {body[:200].decode(errors='replace')}")
    queue = local_path(headers["Location"])
    for _ in range(300):
        item = jenkins.json(queue.rstrip("/") + "/api/json")
        if item and item.get("executable"):
            return item["executable"]["number"], local_path(item["executable"]["url"])
        if item and item.get("cancelled"):
            fail(f"{JOB} was cancelled in the queue")
        time.sleep(1)
    fail(f"{JOB} waited 5 minutes for {NODE}")


def follow(jenkins, build, prefix):
    """Print the build's console as it grows. Returns its result."""
    offset = 0
    while True:
        status, headers, body = jenkins.call("GET", f"{build}logText/progressiveText?start={offset}")
        if status == 200:
            for line in body.decode(errors="replace").splitlines():
                if line.strip():
                    print(f"{prefix}{line}", flush=True)
            offset = int(headers.get("X-Text-Size", offset))
            if headers.get("X-More-Data") != "true":
                break
        time.sleep(1)
    for _ in range(30):
        info = jenkins.json(f"{build}api/json?tree=result,building")
        if info and not info["building"]:
            return info["result"]
        time.sleep(1)
    return "UNKNOWN"


def run(request, prefix="dc-agent | "):
    """Run a request on dc-agent. Returns (exit code, results)."""
    from fleet import parse_results

    jenkins = Jenkins()
    preflight(jenkins)
    request = {**request, "commit": os.environ.get("GIT_COMMIT", ""), "release_build": os.environ.get("BUILD_URL", "")}
    number, build = start(jenkins, request)
    print(f"{prefix}{JOB} #{number} on {NODE}", flush=True)
    result = follow(jenkins, build, prefix)
    status, _, body = jenkins.call("GET", f"{build}artifact/{RESULTS}")
    results = parse_results(body.decode()) if status == 200 else []
    return (0 if result == "SUCCESS" else 1), results
