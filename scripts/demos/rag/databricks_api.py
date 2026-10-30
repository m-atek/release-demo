"""Databricks' REST API for demo 23 (not named databricks: that would hide
the Databricks SDK's package, which MLflow imports): AI Search (Vector Search's new name)
endpoints and indexes, the Files API for the volume, one-time job runs,
permissions, Unity Catalog tables and SQL warehouses.

Two identities, as the spec splits them:
- as_pipeline(): the service principal release-pipeline, by OAuth machine
  to machine, with databricks/client-id and databricks/client-secret from
  SSM. The pipeline, /ask and the tools host's idle stop use it.
- as_you(): your own login, the Databricks CLI profile release-demo
  (databricks auth login), for search.py up and down, bootstrap.sh and
  setup.py on the Mac.

The standard library only, so the tools host's Python environment runs it
as it is. No token or secret is ever printed.
"""

import base64
import configparser
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import fail, get_parameter, ssm_client

USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"
PROFILE = "release-demo"


class DatabricksError(Exception):
    def __init__(self, status, message):
        super().__init__(f"Databricks answered {status}: {message}")
        self.status = status


class Databricks:
    def __init__(self, host, token, client=None):
        self.host = host.rstrip("/")
        self._token = token  # a callable: () -> bearer token
        self.client = client  # (client_id, client_secret) for the service principal, else None
        self._index_tokens, self._lock = {}, threading.Lock()

    @classmethod
    def as_pipeline(cls, ssm=None):
        ssm = ssm or ssm_client()
        host = get_parameter(ssm, "databricks/host")
        client = (get_parameter(ssm, "databricks/client-id"), get_parameter(ssm, "databricks/client-secret"))
        cached = {}

        def token():
            if cached.get("expires", 0) - 60 < time.time():
                cached.update(oauth(host, client))
            return cached["token"]
        return cls(host, token, client)

    @classmethod
    def as_you(cls, profile=PROFILE):
        config = configparser.ConfigParser()
        config.read(Path.home() / ".databrickscfg")
        if profile not in config or not config[profile].get("host"):
            fail(f"No Databricks CLI profile {profile}. Run: databricks auth login --host <workspace URL> "
                 f"--profile {profile}")
        host = config[profile]["host"]

        def token():
            result = subprocess.run(["databricks", "auth", "token", "--profile", profile], capture_output=True,
                                    text=True)
            if result.returncode != 0:
                fail(f"databricks auth token --profile {profile} failed: sign in again with databricks auth login "
                     f"--profile {profile}")
            return json.loads(result.stdout)["access_token"]
        return cls(host, token)

    # HTTP

    def call(self, method, path, body=None, query=None, raw=None, headers=None, base=None, token=None):
        url = (base or self.host) + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token or self._token()}", "User-Agent": USER_AGENT,
            **({"Content-Type": "application/json"} if raw is None else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read()
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")
            try:
                message = json.loads(text).get("message") or text
            except ValueError:
                message = text
            raise DatabricksError(error.code, message[:300])

    def get(self, path, **query):
        return self.call("GET", path, query=query or None)

    def missing_ok(self, method, path, body=None):
        try:
            return self.call(method, path, body)
        except DatabricksError as error:
            if error.status == 404 or "does not exist" in str(error).lower() or "not found" in str(error).lower():
                return None
            raise

    # AI Search: endpoints

    def endpoint(self, name):
        return self.missing_ok("GET", f"/api/2.0/vector-search/endpoints/{name}")

    def create_endpoint(self, name):
        return self.call("POST", "/api/2.0/vector-search/endpoints", {"name": name, "endpoint_type": "STANDARD"})

    def delete_endpoint(self, name):
        return self.missing_ok("DELETE", f"/api/2.0/vector-search/endpoints/{name}")

    def endpoint_permissions(self, endpoint_id, application_id, level="CAN_MANAGE"):
        """Gives the service principal use of the endpoint, without touching anyone else's rights."""
        return self.call("PATCH", f"/api/2.0/permissions/vector-search-endpoints/{endpoint_id}", {
            "access_control_list": [{"service_principal_name": application_id, "permission_level": level}]})

    # AI Search: indexes

    def indexes(self, endpoint):
        found, token = [], None
        while True:
            reply = self.get("/api/2.0/vector-search/indexes", endpoint_name=endpoint,
                             **({"page_token": token} if token else {}))
            found += reply.get("vector_indexes") or []
            token = reply.get("next_page_token")
            if not token:
                return found

    def index(self, name):
        return self.missing_ok("GET", f"/api/2.0/vector-search/indexes/{name}")

    def create_index(self, name, endpoint, table, embedding_model, columns):
        """A Delta Sync index with managed embeddings of the text column, synced when asked."""
        return self.call("POST", "/api/2.0/vector-search/indexes", {
            "name": name, "endpoint_name": endpoint, "primary_key": "chunk_id", "index_type": "DELTA_SYNC",
            "delta_sync_index_spec": {
                "source_table": table, "pipeline_type": "TRIGGERED", "columns_to_sync": columns,
                "embedding_source_columns": [{"name": "text", "embedding_model_endpoint_name": embedding_model}]}})

    def delete_index(self, name):
        return self.missing_ok("DELETE", f"/api/2.0/vector-search/indexes/{name}")

    def sync_index(self, name):
        return self.call("POST", f"/api/2.0/vector-search/indexes/{name}/sync", {})

    def index_token(self, name):
        """A token for one index, for its own query URL (the service principal only), kept until it expires."""
        with self._lock:
            found = self._index_tokens.get(name)
            if not found or found["expires"] - 60 < time.time():
                details = [{"type": "unity_catalog_permission", "securable_type": "table",
                            "securable_object_name": name, "operation": "ReadVectorIndex"}]
                found = oauth(self.host, self.client, authorization_details=details)
                self._index_tokens[name] = found
            return found["token"]

    def query(self, name, body, index_url=None):
        """Query an index: at its own URL with an index-scoped token when the service principal
        asks and the index has one, else through the workspace."""
        if index_url and self.client:
            return self.call("POST", "/query", body, base=f"https://{index_url.rstrip('/')}",
                             token=self.index_token(name))
        return self.call("POST", f"/api/2.0/vector-search/indexes/{name}/query", body)

    # Files, jobs, tables, warehouses

    def upload(self, path, data):
        return self.call("PUT", f"/api/2.0/fs/files{urllib.parse.quote(path)}", query={"overwrite": "true"},
                         raw=data, headers={"Content-Type": "application/octet-stream"})

    def submit_run(self, body):
        return self.call("POST", "/api/2.2/jobs/runs/submit", body)["run_id"]

    def run(self, run_id):
        return self.get("/api/2.2/jobs/runs/get", run_id=run_id)

    def table(self, full_name):
        return self.missing_ok("GET", f"/api/2.1/unity-catalog/tables/{full_name}")

    def warehouse(self, name):
        for warehouse in self.get("/api/2.0/sql/warehouses").get("warehouses") or []:
            if warehouse.get("name") == name:
                return warehouse
        return None


def oauth(host, client, authorization_details=None):
    """An OAuth token for the service principal, machine to machine: {token, expires}."""
    form = {"grant_type": "client_credentials", "scope": "all-apis"}
    if authorization_details:
        form["authorization_details"] = json.dumps(authorization_details)
    basic = base64.b64encode(f"{client[0]}:{client[1]}".encode()).decode()
    request = urllib.request.Request(f"{host.rstrip('/')}/oidc/v1/token", data=urllib.parse.urlencode(form).encode(),
                                     method="POST", headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT,
                                                             "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise DatabricksError(error.code, "the service principal's OAuth sign-in was refused: check "
                                          "databricks/client-id and databricks/client-secret in SSM")
    return {"token": reply["access_token"], "expires": time.time() + int(reply.get("expires_in", 3600))}


def rows(reply):
    """A query's result as dicts, with the score Databricks adds as the last column."""
    names = [column["name"] for column in (reply.get("manifest") or {}).get("columns") or []]
    return [dict(zip(names, row)) for row in (reply.get("result") or {}).get("data_array") or []]


def environment():
    """Which identity this process should use: the pipeline's on the tools host, yours on the Mac."""
    return "pipeline" if os.environ.get("JENKINS_URL") or Path("/etc/release-demo/host.env").exists() else "you"
