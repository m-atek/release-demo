"""ServiceNow's side of the approval bridge (scripts/approval_bridge.py): the
business rule fires only when a run's approval is decided, tells the bridge
only which change, and the setup keeps the token in SSM. Fakes, no ServiceNow.

    .venv/bin/python -m pytest -q scripts/tests/test_approval_bridge_rule.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import approval_bridge  # noqa: E402


def test_the_rule_fires_when_the_approval_of_a_github_or_azure_run_is_decided():
    condition = approval_bridge.condition()
    assert condition.startswith("current.approval.changes() && (current.approval == 'approved' || current.approval == 'rejected')")
    assert "indexOf('github-actions:') == 0" in condition and "indexOf('azure-pipelines:') == 0" in condition


def test_the_call_names_the_change_only_and_carries_the_token():
    script = approval_bridge.SCRIPT
    assert "request.setRequestBody(JSON.stringify({sys_id: current.getUniqueValue()}));" in script
    assert "'Bearer ' + token" in script and "+ '/servicenow'" in script
    assert "gs.getProperty('release_demo.approval_bridge.token', '')" in script


class SSM:
    exceptions = SimpleNamespace(ParameterNotFound=KeyError)

    def __init__(self, values):
        self.values, self.put = dict(values), {}

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.values:
            raise KeyError(Name)
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name, Value, Overwrite, Type):
        self.put[Name] = (Value, Type)
        self.values[Name] = Value


class ServiceNow:
    def __init__(self):
        self.ensured, self.calls = [], []

    def ensure(self, table, query, values, label):
        self.ensured.append((table, values))

    def find(self, table, query, fields=None):
        return None

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))


def test_before_the_bridge_is_applied_nothing_is_made(capsys):
    sn = ServiceNow()
    approval_bridge.setup(sn, SSM({}))
    assert sn.ensured == [] and sn.calls == []
    assert "isn't applied yet" in capsys.readouterr().out


def test_the_token_is_generated_into_ssm_once_and_set_as_an_encrypted_property():
    ssm = SSM({"/release-demo/bridge/url": "https://bridge.example/", "/release-demo/bridge/servicenow-token": "unset"})
    sn = ServiceNow()
    approval_bridge.setup(sn, ssm)
    token, kind = ssm.put["/release-demo/bridge/servicenow-token"]
    assert kind == "SecureString" and len(token) > 30
    (method, path, body), = sn.calls
    assert (method, path, body["type"], body["value"]) == ("POST", "/api/now/table/sys_properties", "password2", token)
    url, rule = sn.ensured
    assert url[1]["value"] == "https://bridge.example" and rule[0] == "sys_script"
    assert rule[1]["when"] == "after" and rule[1]["action_update"] == "true" and rule[1]["condition"] == approval_bridge.condition()
    approval_bridge.setup(ServiceNow(), ssm)
    assert ssm.put["/release-demo/bridge/servicenow-token"][0] == token  # kept, not made again
