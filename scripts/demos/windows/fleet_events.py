"""What the fleet tells Grafana and Splunk.

Grafana: one tile per server (its version and status: waiting, upgrading,
healthy, failed, rolled back) and the wave progress, as JSON lines in Grafana
Cloud Logs under kind="fleet". The store-sync row of grafana/releases.json
shows the latest line per server. Splunk: one event per server per run, with
the release event's fields, so the change's Splunk search finds them.

An event that can't be sent never stops a release (events.send).
"""

import base64
import json
import sys
import threading
import time
from pathlib import Path

# The shared scripts, after anything already on the path: a demo module of the same name wins.
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parents[2]))
import events  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402

_lock = threading.Lock()
_cache = {}


def _ssm():
    with _lock:
        if "ssm" not in _cache:
            _cache["ssm"] = ssm_client()
        return _cache["ssm"]


def _loki():
    with _lock:
        if "loki" not in _cache:
            ssm = _cache.get("ssm") or ssm_client()
            _cache["ssm"] = ssm
            url = get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push"
            auth = base64.b64encode(f"{get_parameter(ssm, 'grafana/logs-user')}:"
                                    f"{get_parameter(ssm, 'grafana/logs-token')}".encode()).decode()
            _cache["loki"] = (url, auth)
        return _cache["loki"]


def to_grafana(service, body):
    url, auth = _loki()
    labels = {"source": "release-demo", "service": service, "environment": "production", "kind": "fleet"}
    stream = {"stream": labels, "values": [[str(time.time_ns()), json.dumps(body)]]}
    return events.send(url, {"streams": [stream]}, {"Authorization": f"Basic {auth}"})


def tile(service, server, status, version, wave=None, change=None):
    """A server's tile: its version and status."""
    return to_grafana(service, {"type": "server", "server": server, "status": status, "version": version,
                                "wave": wave, "change": change})


def progress(service, label, done, total, target):
    """The wave progress above the tiles: "Wave 3 of 4: stopped" and "4 of 6 on 3.3.0"."""
    return to_grafana(service, {"type": "wave", "label": label, "progress": f"{done} of {total} on {target}"})


def to_splunk(state, result):
    """One event per server: the release event's fields plus the server's own."""
    body = {
        "build": state["build"]["number"], "build_url": state["build"].get("url"), "service": state["service"],
        "version": state["version"], "environment": "production", "change": state.get("change", {}).get("number"),
        "jira_key": state["jira_key"], "stage": "Production", "result": result["status"],
        "duration_seconds": round(result["seconds"], 1), "requested_by": state.get("requested_by"),
        "option": state.get("option"), "server": result["server"], "store": result["store"], "wave": result["wave"],
        "version_before": result.get("before"), "version_after": result.get("after"), "sha256": result.get("sha256"),
        "prechecks": {what: ok for what, ok in result.get("prechecks", [])}, "health": result.get("health"),
        "rollback": result.get("rollback"), "commands": result.get("commands", []),
    }
    return events.to_splunk(_ssm(), body)
