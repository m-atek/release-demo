"""What demo 19's tools-host scripts share: production's folder in Git, the
difference between two versions of its values, the image JFrog holds for
1.9.1, and the drift incidents in ServiceNow.

fix.py and reset.py run on the tools-host agent, where JFrog Container
Registry answers on localhost and Argo CD on the cluster host's private
address. The records they write name the same CI as demo 8's releases,
catalog-production.
"""

import json
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parents[1]))
sys.path.append(str(HERE.parent / "kubernetes"))  # demo 8's registry client, last so its names shadow nothing
from common import ROOT, fail, service  # noqa: E402

SERVICE = "catalog"
INCIDENT = re.compile(r"\bINC\d{7,}\b")
EDITOR = re.compile(r"^Edited by (\S+)", re.MULTILINE)
DRIFT_PREFIX = "argocd-drift:"
# Values keys, as the deployment's fields Argo CD shows and the records name.
MANIFEST_PATHS = {"replicas": "spec.replicas", "image.digest": "image", "image.repository": "image",
                  "resources.requests.cpu": "resources.requests.cpu", "resources.limits.cpu": "resources.limits.cpu",
                  "resources.requests.memory": "resources.requests.memory",
                  "resources.limits.memory": "resources.limits.memory"}
RESOLVED, CLOSED, CANCELED = "6", "7", "8"
SOLVED = ["solved (permanently)", "solved permanently", "resolved by change", "solution provided"]


def settings():
    return service(SERVICE)["gitops"]


def values_path():
    return f"{settings()['path']}/values.yaml"


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value


def values_diff(before, after):
    """[{key, path, from, to}] for each value the second text changes."""
    old, new = dict(flatten(yaml.safe_load(before or "") or {})), dict(flatten(yaml.safe_load(after or "") or {}))
    changes = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changes.append({"key": key, "path": MANIFEST_PATHS.get(key, key), "from": old.get(key), "to": new.get(key)})
    return changes


def diff_text(changes):
    """spec.replicas 5 (was 3): what the records say a change does."""
    return ", ".join(f"{c['path']} {c['to']} (was {c['from']})" for c in changes) or "no values changed"


def short_diff(changes):
    """spec.replicas 5: the resolution note's form."""
    return ", ".join(f"{c['path']} {c['to']}" for c in changes) or "no values changed"


def image_repository(values):
    """docker-local/catalog, the registry path of artifactory-demo.es.nl/docker-local/catalog."""
    return values["image"]["repository"].split("/", 1)[1]


def release_digest(token):
    """1.9.1's image digest, as its release copy in JFrog names it: demos-releases/catalog/1.9.1/build-info.json."""
    import publish

    version = settings()["version"]
    path = f"{publish.releases_root(SERVICE)}/{version}/build-info.json"
    status, body = publish.request("GET", path, token)
    if status != 200:
        fail(f"JFrog Container Registry has no {path} ({status}): release catalog {version} once (demo 8) first.")
    return json.loads(body)["image_digest"]


def image_in_jfrog(values, token, registry_url=None):
    """True when JCR's Docker registry has the manifest the values pin."""
    import oci

    digest = values["image"].get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return False, f"image.digest {digest or '(empty)'} isn't a sha256 digest"
    registry = oci.Registry(registry_url or os.environ.get("JCR_URL", "http://127.0.0.1:8082"), bearer=token)
    repository = image_repository(values)
    found = registry.tagged(repository, digest)
    if found != digest:
        return False, f"{digest[:19]}... isn't in {repository}"
    return True, f"{repository}@{digest[:19]}... is in JFrog"


# ServiceNow: the drift incidents the event bridge opens.

def incident_in(title):
    found = INCIDENT.search(title or "")
    return found.group(0) if found else None


def editor(description):
    """The cluster user the bridge recorded: "Edited by pieter-oncall ..." in the description."""
    found = EDITOR.search(description or "")
    return found.group(1) if found else None


def person_for(kubernetes_user):
    everyone = yaml.safe_load((ROOT / "people.yaml").read_text())
    for key, person in everyone.items():
        if person.get("kubernetes_user") == kubernetes_user:
            return key, person
    return None, None


def open_drift_incidents(sn):
    return sn.call("GET", "/api/now/table/incident", sysparm_query=f"correlation_idSTARTSWITH{DRIFT_PREFIX}"
                   f"^stateNOT IN{RESOLVED},{CLOSED},{CANCELED}^ORDERBYnumber",
                   sysparm_fields="sys_id,number,short_description,state", sysparm_limit=50) or []


def solved_code(sn):
    """The instance's own "solved" close code for incidents, read from its choices."""
    choices = sn.call("GET", "/api/now/table/sys_choice", sysparm_query="name=incident^element=close_code^inactive=false",
                      sysparm_fields="value,label", sysparm_limit=50) or []
    by_label = {c["label"].lower(): c["value"] for c in choices}
    by_value = {c["value"].lower(): c["value"] for c in choices}
    for wanted in SOLVED:
        if wanted in by_label or wanted in by_value:
            return by_label.get(wanted) or by_value[wanted]
    for label, value in by_label.items():
        if "solv" in label:
            return value
    fail("ServiceNow lists no close code for incidents that means solved.")


def resolve(sn, incident_id, notes):
    sn.call("PATCH", f"/api/now/table/incident/{incident_id}",
            {"state": RESOLVED, "close_code": solved_code(sn), "close_notes": notes})
