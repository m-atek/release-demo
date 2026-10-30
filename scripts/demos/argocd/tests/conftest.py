import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(ROOT / "scripts"))
sys.path.append(str(HERE / "host"))
sys.path.append(str(HERE.parent / "terraform"))

import run_state  # noqa: E402


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    return run_state.PATH


def audit(user, at, verb="patch", subresource=None, body=None, groups=None, uri=None, agent="kubectl/v1.34.1",
          name="catalog"):
    """One audit event as k3s's API server writes it at RequestResponse."""
    ref = {"resource": "deployments", "namespace": "production", "name": name, "apiGroup": "apps", "apiVersion": "v1"}
    if subresource:
        ref["subresource"] = subresource
    return {"kind": "Event", "apiVersion": "audit.k8s.io/v1", "level": "RequestResponse", "stage": "ResponseComplete",
            "verb": verb, "requestURI": uri or f"/apis/apps/v1/namespaces/production/deployments/{name}"
                                              f"{'/' + subresource if subresource else ''}",
            "user": {"username": user, "groups": groups or []}, "userAgent": agent, "objectRef": ref,
            "responseStatus": {"code": 200}, "requestObject": body or {},
            "requestReceivedTimestamp": at, "stageTimestamp": at}


def application(history, finished="2026-10-30T10:15:14Z", automated=True, phase="Succeeded"):
    return {"metadata": {"name": "catalog-production"},
            "status": {"history": [{"revision": r, "deployedAt": t} for r, t in history],
                       "operationState": {"phase": phase, "startedAt": "2026-10-30T10:15:12Z", "finishedAt": finished,
                                          "operation": {"initiatedBy": {"automated": automated}},
                                          "syncResult": {"revision": history[-1][0], "resources": [
                                              {"group": "apps", "kind": "Deployment", "namespace": "production",
                                               "name": "catalog", "status": "Synced"}]}}}}


@pytest.fixture
def deployment():
    return {"spec": {"replicas": 3, "template": {"spec": {"containers": [{"name": "catalog"}]}}}}


def load_json(path):
    return json.loads(Path(path).read_text())
