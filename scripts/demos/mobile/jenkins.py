"""Start the store-floor-build job and collect what it built.

The release's Build stage, a rollback and reset.py all need a build on two
agents: iOS on the Mac, Android on the tools host. The job does that, in
parallel, and archives the signed files; this module starts it as release-bot
from the tools host, waits, and downloads them.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import mobile

JENKINS = os.environ.get("JENKINS_LOCAL_URL", "http://localhost:8080")


class Jenkins:
    def __init__(self, url=JENKINS, user="release-bot", token=None):
        token = token or mobile.get_parameter(mobile.ssm_client(), "jenkins/release-bot-api-token")
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()

    def call(self, method, path, data=None, raw=False):
        if path.startswith("http"):
            # Jenkins names a build by its public address, behind Cloudflare,
            # which turns this client away (403, error 1010): stay on self.url.
            parts = urllib.parse.urlsplit(path)
            path = parts.path + (f"?{parts.query}" if parts.query else "")
        url = self.url + path
        request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return response, body if raw else (json.loads(body) if body else None)
        except urllib.error.HTTPError as error:
            mobile.fail(f"Jenkins {method} {path}: {error.code} {error.read()[:200].decode(errors='replace')}")
        except urllib.error.URLError as error:
            mobile.fail(f"Jenkins doesn't answer at {self.url}: {error.reason}")

    def agents_online(self, label):
        _, reply = self.call("GET", f"/label/{urllib.parse.quote(label)}/api/json?tree=nodes[nodeName,offline]")
        return [n["nodeName"] for n in reply.get("nodes", []) if not n.get("offline")]

    def start(self, job, parameters):
        response, _ = self.call("POST", f"/job/{job}/buildWithParameters",
                                urllib.parse.urlencode(parameters).encode(), raw=True)
        return response.headers["Location"].rstrip("/") + "/api/json"

    def wait(self, queue_item, timeout, interval=5, say=print):
        """The build's number once it starts, then its result once it ends."""
        deadline = time.time() + timeout
        number = None
        while time.time() < deadline:
            _, item = self.call("GET", queue_item)
            if item.get("cancelled"):
                mobile.fail("The build job was cancelled in the queue.")
            if item.get("executable"):
                number = item["executable"]["number"]
                url = item["executable"]["url"]
                say(f"store-floor-build #{number} started: {url}")
                break
            time.sleep(interval)
        else:
            mobile.fail(f"The build job didn't start within {timeout // 60} minutes. Is the Mac agent connected?")
        while time.time() < deadline:
            _, build = self.call("GET", f"{url}api/json?tree=result,building,artifacts[relativePath]")
            if not build["building"] and build["result"]:
                return number, url, build
            time.sleep(interval)
        mobile.fail(f"store-floor-build #{number} didn't finish within {timeout // 60} minutes: {url}")

    def download(self, build_url, artifacts, target):
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        saved = []
        for artifact in artifacts:
            _, body = self.call("GET", f"{build_url}artifact/{urllib.parse.quote(artifact['relativePath'])}", raw=True)
            path = target / Path(artifact["relativePath"]).name
            path.write_bytes(body)
            saved.append(path)
        return saved


def build(version, build_number, source, platforms, orders_url, timeout=40 * 60, jenkins=None, say=print):
    """Run store-floor-build and return the folder with its files, or stop the run."""
    jenkins = jenkins or Jenkins()
    config = mobile.config()
    job = config["app"]["build_job"]
    if "ios" in platforms and not jenkins.agents_online(config["app"]["ios_agent"]):
        mobile.fail(f"No Jenkins agent with the label {config['app']['ios_agent']} is connected, so iOS can't build. "
                    "Start the Mac agent (demo 6), then run again.")
    queue = jenkins.start(job, {"VERSION": version, "STORE_BUILD": build_number, "SOURCE": source,
                                "PLATFORMS": ",".join(platforms), "ORDERS_URL": orders_url,
                                "REQUESTED_BY": os.environ.get("BUILD_TAG", "by hand")})
    say(f"Started {job} for {version} ({build_number}), {', '.join(platforms)}: waiting for it")
    number, url, result = jenkins.wait(queue, timeout, say=say)
    if result["result"] != "SUCCESS":
        mobile.fail(f"{job} #{number} ended {result['result']}: {url}console")
    target = mobile.ROOT / "dist" / mobile.SERVICE / "parts"
    files = jenkins.download(url, result["artifacts"], target)
    say(f"{job} #{number}: {len(files)} files from {url}")
    return target
