"""The drift recorder on the cluster host: each time Argo CD puts production
back after a hand edit, who made the edit, what it changed and when, from the
cluster's own audit log, to the event bridge.

    recorder.py serve    the systemd unit release-demo-recorder: listens on the node, port 9095
    recorder.py last     the event for the application's last sync, printed and not sent

Argo CD's notifications controller calls it after every automated sync
that succeeded (the trigger on-automated-sync), sending the application and
the sync's finish time, with the token setup made. The recorder reads the
rest from the cluster as root, and writes nothing to it:

- the application's last two syncs, and the resources the last one touched;
- from the audit log, every write to those resources since the sync before,
  by anyone but Argo CD's application controller and Kubernetes' own
  controllers: the user and groups, the time to the microsecond, kubectl's
  field manager and the request (for a scale, {"spec":{"replicas":5}}); then
  Argo CD's first write after the last of them, the one that put it back;
- the diff against Git: each field an edit set, with the edit's value and
  the value Argo CD put back, which is Git's;
- the seconds from the first edit to Argo CD's write, rounded down;
- when Argo CD saw the application go OutOfSync, from its own event;
- for a break-glass user, the certificate's issue time and expiry, from its
  approved CertificateSigningRequest.

A sync with no hand edit before it is Git's doing (the fix pipeline, reset.py
or a commit to main) and sends nothing. The audit log decides, not the Git
revision: an unrelated commit to main moves the application's revision
without a sync, so the self-heal after it is at a new revision.

One event of kind drift goes to the bridge (POST /events), which opens the
incident and posts to Slack and Grafana. Changes through Git, by the fix
pipeline or reset.py, sync at a new revision: no trigger, nothing sent.
"""

import argparse
import base64
import datetime
import hmac
import http.server
import ipaddress
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))
from common import service  # noqa: E402

PORT = 9095
TOKEN = Path("/etc/release-demo/recorder-token")
AUDIT_DIR = Path("/var/lib/rancher/k3s/server/logs")
STATE = Path("/var/lib/release-demo/recorder-last.json")
ARGOCD_CONTROLLER = "system:serviceaccount:argocd:argocd-application-controller"
BREAK_GLASS_GROUP = "release-demo:break-glass"
WRITES = {"create", "update", "patch", "delete"}
# Kubernetes' own writes to a deployment (its revision annotation) are not edits.
MACHINES = ("system:serviceaccount:kube-system:", "system:kube-controller-manager", "system:apiserver",
            "system:node:")
CALLERS = [ipaddress.ip_network("10.42.0.0/16"), ipaddress.ip_network("127.0.0.0/8")]  # k3s's pods, and the node
SSM_PREFIX = "/release-demo/"


def parse_time(text):
    """An RFC 3339 time with up to nanoseconds, as an aware UTC datetime (microseconds kept)."""
    text = text.strip().replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    return datetime.datetime.fromisoformat(text).astimezone(datetime.timezone.utc)


def stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# What Argo CD's application says.

def automated_sync(app):
    """(True, "") for an automated sync that succeeded, after one before it; else (False, why not)."""
    status = app.get("status") or {}
    operation = status.get("operationState") or {}
    if operation.get("phase") != "Succeeded":
        return False, f"the last sync is {operation.get('phase') or 'missing'}"
    if not (operation.get("operation") or {}).get("initiatedBy", {}).get("automated"):
        return False, "the last sync wasn't automated"
    if len(status.get("history") or []) < 2:
        return False, "the application's first sync"
    return True, ""


def touched(app):
    """The (group, resource, namespace, name) of each resource the last sync touched."""
    found = set()
    for item in app["status"]["operationState"].get("syncResult", {}).get("resources") or []:
        kind = item.get("kind", "")
        found.add((item.get("group", ""), kind.lower() + "s", item.get("namespace", ""), item.get("name", "")))
    return found


# The audit log.

def audit_events(directory=AUDIT_DIR):
    """Every event in the audit log and its rotated files, oldest file first."""
    files = sorted(directory.glob("audit*.log"), key=lambda p: p.stat().st_mtime)
    for path in files:
        with open(path, errors="replace") as file:
            for line in file:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def when(event):
    return parse_time(event.get("requestReceivedTimestamp") or event["stageTimestamp"])


def target(event):
    ref = event.get("objectRef") or {}
    return (ref.get("apiGroup", ""), ref.get("resource", ""), ref.get("namespace", ""), ref.get("name", ""))


def is_write(event):
    status = (event.get("responseStatus") or {}).get("code", 200)
    return event.get("verb") in WRITES and event.get("stage", "ResponseComplete") == "ResponseComplete" and status < 400


def machine(username):
    return username == ARGOCD_CONTROLLER or username.startswith(MACHINES)


def field_manager(event):
    """kubectl's field manager from the request, or what the user agent and the call say."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(event.get("requestURI", "")).query)
    if query.get("fieldManager"):
        return query["fieldManager"][0]
    agent = event.get("userAgent", "")
    if agent.startswith("kubectl"):
        sub = (event.get("objectRef") or {}).get("subresource")
        return f"kubectl-{sub}" if sub == "scale" else f"kubectl-{event.get('verb', 'edit')}"
    return agent.split(" ")[0] or "unknown"


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value


def lookup(document, path):
    for part in path.split("."):
        if not isinstance(document, dict) or part not in document:
            return None
        document = document[part]
    return document


def edited_fields(event):
    """The fields an edit set, as dotted paths: spec.replicas for a scale. Metadata and status aren't the app's state."""
    body = event.get("requestObject") or {}
    return [(path, value) for path, value in flatten(body)
            if path and not path.startswith(("metadata", "status", "apiVersion", "kind"))]


# The certificate behind a break-glass user.

def openssl(*args, stdin):
    return subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True).stdout


def certificate_of(csr):
    """(common name, not after) of a signed CSR's certificate, or None."""
    pem = (csr.get("status") or {}).get("certificate")
    if not pem:
        return None
    text = base64.b64decode(pem).decode()
    subject = openssl("x509", "-noout", "-subject", "-nameopt", "RFC2253", stdin=text)
    dates = openssl("x509", "-noout", "-enddate", stdin=text)
    names = dict(part.split("=", 1) for part in subject.strip().removeprefix("subject=").split(",") if "=" in part)
    end = dates.strip().removeprefix("notAfter=")
    try:
        expires = datetime.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return names.get("CN"), expires


def certificate(csrs, user, at, read=None):
    """The break-glass certificate user held at that moment: when it was approved and when it ends."""
    read = read or certificate_of
    for csr in sorted(csrs, key=lambda c: c["metadata"].get("creationTimestamp", ""), reverse=True):
        signed = read(csr)
        if not signed or signed[0] != user:
            continue
        approved = [c for c in (csr.get("status") or {}).get("conditions") or [] if c.get("type") == "Approved"]
        issued = parse_time(approved[0].get("lastUpdateTime") or csr["metadata"]["creationTimestamp"]) if approved else None
        if issued and issued <= at <= signed[1]:
            return {"user": user, "issued": stamp(issued), "expires": stamp(signed[1])}
    return None


def out_of_sync(app_events, after, before):
    """When Argo CD recorded the application going OutOfSync, between the edit and the revert."""
    times = []
    for event in app_events:
        if "-> OutOfSync" in event.get("message", ""):
            moment = event.get("eventTime") or event.get("lastTimestamp") or event.get("firstTimestamp")
            if moment:
                moment = parse_time(moment)
                # Kubernetes events have whole seconds: the edit's own second counts.
                if after.replace(microsecond=0) <= moment <= before:
                    times.append(moment)
    return stamp(min(times)) if times else None


# The event.

def build_event(app, audit, live, csrs, app_events, config):
    """The drift event for the application's last sync, or (None, why not)."""
    synced, why = automated_sync(app)
    if not synced:
        return None, why
    status = app["status"]
    previous = parse_time(status["history"][-2]["deployedAt"])
    finished = parse_time(status["operationState"]["finishedAt"])
    resources = touched(app)
    since, until = previous - datetime.timedelta(seconds=1), finished + datetime.timedelta(seconds=5)
    # A scale is a write to the deployment's subresource: objectRef names the deployment itself.
    writes = [e for e in audit if is_write(e) and target(e) in resources and since <= when(e) <= until]
    hand = sorted((e for e in writes if not machine(e["user"].get("username", ""))), key=when)
    if not hand:
        return None, "no hand edit since the sync before: Git's change"
    argocd = sorted((e for e in writes if e["user"].get("username") == ARGOCD_CONTROLLER and when(e) >= when(hand[-1])),
                    key=when)
    if not argocd:
        return None, "no write by Argo CD after the hand edit"
    revert = argocd[0]
    set_by_hand = {}
    for edit in hand:
        for path, value in edited_fields(edit):
            set_by_hand[path] = value
    diff = [{"path": path, "live": value, "git": lookup(live, path)} for path, value in set_by_hand.items()
            if lookup(live, path) != value]
    if not diff:
        return None, "the hand edit set what Git says: nothing was put back"
    first = hand[0]
    edits = []
    for edit in hand:
        ref = edit.get("objectRef") or {}
        edits.append({"user": edit["user"].get("username"), "groups": edit["user"].get("groups") or [],
                      "at": stamp(when(edit)), "verb": edit.get("verb"), "field_manager": field_manager(edit),
                      "user_agent": edit.get("userAgent", ""),
                      "resource": f"{ref.get('resource', '')}/{ref.get('name', '')}",
                      "request": edit.get("requestObject") or {}})
    user = first["user"].get("username")
    break_glass = BREAK_GLASS_GROUP in (first["user"].get("groups") or [])
    ref = revert.get("objectRef") or {}
    gitops = config["gitops"]
    event = {
        "kind": "drift",
        "id": f"{app['metadata']['name']}:{status['operationState']['finishedAt']}",
        "application": app["metadata"]["name"],
        "service": config["name"],
        "namespace": gitops["namespace"],
        "resource": f"deployment/{ref.get('name') or gitops['deployment']}",
        "path": gitops["path"],
        "cmdb_item": config["servicenow"]["cmdb_item"],
        "revision": status["operationState"].get("syncResult", {}).get("revision", ""),
        "edits": edits,
        "diff": diff,
        "out_of_sync_at": out_of_sync(app_events, when(first), when(revert)),
        "reverted_at": stamp(when(revert)),
        "seconds": int((when(revert) - when(first)).total_seconds()),
        "break_glass": break_glass,
        "certificate": certificate(csrs, user, when(first)) if break_glass else None,
    }
    return event, ""


# The cluster, read as root.

def kubectl_json(*args):
    result = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def gather(application):
    config = service("catalog")
    gitops = config["gitops"]
    app = kubectl_json("get", "application", application, "-n", "argocd")
    live = kubectl_json("get", "deployment", gitops["deployment"], "-n", gitops["namespace"])
    csrs = kubectl_json("get", "csr", "-l", "release-demo/break-glass=true")["items"]
    events = kubectl_json("get", "events", "-n", "argocd",
                          "--field-selector", f"involvedObject.kind=Application,involvedObject.name={application}")["items"]
    return build_event(app, audit_events(), live, csrs, events, config)


def ssm_value(name):
    import boto3

    value = boto3.client("ssm", region_name="eu-west-1").get_parameter(Name=SSM_PREFIX + name, WithDecryption=True)
    return value["Parameter"]["Value"]


def send(event, attempts=4):
    """POST the event to the bridge; retried, because the incident matters more than the wait.
    The bridge finds a retried event's incident instead of opening a second."""
    url = json.loads(ssm_value("cluster-host/settings"))["bridge_url"].rstrip("/") + "/events"
    token = ssm_value("bridge/events-token")
    body = json.dumps(event).encode()
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "User-Agent": "release-demo-drift-recorder/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")[:300]
            if error.code < 500:
                raise RuntimeError(f"the bridge refused the event ({error.code}): {text}")
            print(f"  the bridge answered {error.code} (attempt {attempt}): {text}", flush=True)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            print(f"  the bridge didn't answer (attempt {attempt}): {error}", flush=True)
        time.sleep(5 * attempt)
    raise RuntimeError("the bridge didn't take the event")


def record(notice):
    application = notice.get("application", "")
    print(f"automated sync of {application} at {notice.get('finished_at')}", flush=True)
    event, why = gather(application)
    if event is None:
        print(f"  nothing to record: {why}", flush=True)
        return None
    print(f"  {event['edits'][0]['user']}: {', '.join(d['path'] + ' ' + str(d['live']) for d in event['diff'])}; "
          f"put back after {event['seconds']}s", flush=True)
    result = send(event)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"at": time.time(), "incident": result.get("incident"), "id": event["id"]}))
    print(f"  recorded: {result.get('incident')}", flush=True)
    return result


# The listener.

class Handler(http.server.BaseHTTPRequestHandler):
    token = ""
    work = None

    def log_message(self, *args):
        pass

    def reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    callers = CALLERS

    def do_POST(self):
        caller = ipaddress.ip_address(self.client_address[0])
        if not any(caller in network for network in self.callers):
            return self.reply(403)
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {self.token}".encode()):
            return self.reply(401)
        try:
            notice = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.reply(400)
        self.work.put(notice)
        self.reply(202)


def worker(work):
    while True:
        notice = work.get()
        try:
            record(notice)
        except Exception as error:  # the next self-heal still gets recorded
            print(f"  failed: {error.__class__.__name__}: {error}", flush=True)


def serve():
    address = json.loads(ssm_value("cluster-host/settings"))["address"]
    Handler.token = TOKEN.read_text().strip()
    Handler.callers = [*CALLERS, ipaddress.ip_network(f"{address}/32")]  # a pod's call may arrive from the node's address
    Handler.work = queue.Queue()
    threading.Thread(target=worker, args=(Handler.work,), daemon=True).start()
    server = http.server.ThreadingHTTPServer((address, PORT), Handler)
    print(f"drift recorder on {address}:{PORT}", flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("last")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run as root on the cluster host: the audit log is root's.")
    if args.command == "serve":
        serve()
        return
    event, why = gather(service("catalog")["gitops"]["application"])
    print(json.dumps(event, indent=2) if event else f"Nothing to record: {why}")


if __name__ == "__main__":
    main()
