"""Video 18's switch, its Jira issue, services.yaml's numbers, and what up applies."""

import importlib.util

import costs
import demo
import setup


def load_switch():
    spec = importlib.util.spec_from_file_location("cost_switch", costs.HERE / "switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SWITCH


class Ssm:
    def __init__(self, value):
        self.value = value

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.value}}


def test_the_switch_applies_only_to_a_service_that_names_a_cost_check():
    switch = load_switch()
    assert switch.name == "cost-check/enabled"
    assert switch.on_for("orders-service", Ssm("on")) and not switch.on_for("orders-service", Ssm("off"))
    assert not switch.on_for("pricing-api", Ssm("on"))


def test_services_yaml_holds_the_numbers_the_video_shows():
    settings = costs.settings("orders-service")
    assert settings == {"limit_usd_month": 100, "approvers": "Cost approvers",
                        "sample": {"page_views": 50, "path": "/", "concurrency": 5},
                        "expected_month": {"page_views": 3_000_000, "new_orders": 60_000}}
    assert costs.service("orders-service")["scripts"]["cost_check"] == "scripts/demos/cost_gate/cost_check.py"


class FakeJira:
    def __init__(self, issues):
        self.issues, self.asked = issues, []

    def call(self, method, path, body=None):
        self.asked.append(path)
        return {"issues": [{"key": key, "fields": {"summary": summary}} for key, summary in self.issues]}


def test_the_issue_is_found_again_by_its_exact_summary():
    jira = FakeJira([("ORD-95", "Show a delivery message for each order page"),
                     ("ORD-96", "Show a delivery message for each order")])
    assert setup.find_issue(jira) == "ORD-96"
    assert "summary" in jira.asked[0] and setup.find_issue(FakeJira([])) is None


def test_up_applies_only_what_is_missing(monkeypatch, capsys):
    applied = []
    found = [(True, "table orders-service-messages-staging, TTL on ttl"), (False, "policy x on y: not there"),
             (True, "orders-service-staging timeout 30 s (1.7.0 needs 30)"), (True, "switch cost-check/enabled: off")]
    monkeypatch.setattr(demo, "checks", lambda: found)
    monkeypatch.setattr(demo.subprocess, "run", lambda command, check: applied.append(command[0]))
    demo.up()
    assert applied == [str(costs.HERE / "tf.sh")]
    found[2] = (False, "orders-service-staging timeout 10 s (1.7.0 needs 30)")
    applied.clear()
    demo.up()
    assert applied == [str(costs.HERE / "tf.sh"), str(demo.ROOT / "scripts" / "tf.sh")]
    assert demo.show(strict=True) == 1 and "FAIL policy x on y" in capsys.readouterr().out
