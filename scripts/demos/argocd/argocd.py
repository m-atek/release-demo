"""Argo CD's API on the cluster host, as the local account release-pipeline.

Used from the tools host by the fix pipeline and reset.py: Argo CD answers on
the cluster host's NodePort 30080, which only the tools host may reach. The
account may read and sync catalog-production and nothing more; its token is
argocd/pipeline-token in SSM. Both callers only ask Argo CD to look at Git
now instead of at its next poll: auto-sync applies what main says.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from common import fail, get_parameter, ssm_client

PORT = 30080


class ArgoCD:
    def __init__(self, ssm=None, base=None, token=None):
        ssm = ssm or (None if base and token else ssm_client())
        self.base = (base or f"http://{get_parameter(ssm, 'cluster-host/address')}:{PORT}").rstrip("/")
        self.token = token or get_parameter(ssm, "argocd/pipeline-token")

    def call(self, path):
        request = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                fail(f"Argo CD refused release-pipeline ({error.code}): is argocd/pipeline-token current? "
                     "scripts/demos/argocd/host/cluster.py up makes a new one.")
            fail(f"Argo CD answered {error.code} for {path}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            fail(f"Argo CD doesn't answer at {self.base} ({getattr(error, 'reason', error)}). "
                 "Is the cluster host up? scripts/demo.sh up argocd")

    def application(self, name, refresh=False):
        """The application; with refresh, after asking Argo CD to compare it with Git now."""
        query = "?refresh=normal" if refresh else ""
        return self.call(f"/api/v1/applications/{urllib.parse.quote(name)}{query}")

    def wait(self, name, revision, timeout, interval=2, clock=time.monotonic, sleep=time.sleep, seen=None):
        """Refresh, then wait until the application is Synced at revision and Healthy.
        Returns the application, or fails with where it stood."""
        app = self.application(name, refresh=True)
        deadline = clock() + timeout
        while True:
            state = summary(app)
            if seen:
                seen(state)
            if state["sync"] == "Synced" and state["revision"] == revision and state["health"] == "Healthy":
                return app
            if clock() >= deadline:
                fail(f"{name} isn't Synced and Healthy at {revision[:7]} after {timeout}s: it is "
                     f"{state['sync']} at {state['revision'][:7] or '?'}, {state['health']}. {state['message']}".strip())
            sleep(interval)
            app = self.application(name)


def summary(app):
    status = app.get("status") or {}
    sync, health = status.get("sync") or {}, status.get("health") or {}
    operation = status.get("operationState") or {}
    conditions = [c.get("message", "") for c in status.get("conditions") or []]
    return {"sync": sync.get("status", "Unknown"), "revision": sync.get("revision") or "",
            "health": health.get("status", "Unknown"), "phase": operation.get("phase", ""),
            "finished_at": operation.get("finishedAt", ""), "message": (conditions[0] if conditions else "")[:200]}

