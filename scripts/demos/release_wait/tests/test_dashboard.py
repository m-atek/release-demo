"""The one panel: a bar per stage of one run, in the pipeline's order, on one scale, read from events.py's events."""

import json
import sys

import pytest

import dashboard
import run_state
from conftest import HERE

DASHBOARD = dashboard.build()
BARS = next(panel for panel in DASHBOARD["panels"] if panel["type"] == "bargauge")


def test_the_page_is_where_the_other_demos_pages_are():
    assert DASHBOARD["uid"] == dashboard.UID == "release-demo-release-wait"
    assert DASHBOARD["title"] == "Where a release waits"
    assert dashboard.FOLDER_UID == "release-demo-dashboards"
    assert DASHBOARD["time"] == {"from": "now-14d", "to": "now"}


def test_the_bars_follow_the_pipeline_s_twelve_stages_in_order():
    assert [target["legendFormat"] for target in BARS["targets"]] == run_state.STAGES
    assert len({target["refId"] for target in BARS["targets"]}) == 12
    for target, stage in zip(BARS["targets"], run_state.STAGES):
        assert f'stage="{stage}"' in target["expr"]


@pytest.mark.parametrize("target", BARS["targets"], ids=lambda target: target["legendFormat"])
def test_each_bar_reads_one_run_s_own_events(target):
    expr = target["expr"]
    assert target["queryType"] == "instant" and target["datasource"] == {"type": "loki", "uid": "grafanacloud-logs"}
    assert expr.startswith('last_over_time({source="release-demo", service="orders-service"} | json | job!="tests" | build="$build"')
    assert 'result="ok"' in expr and "[$__range]) by (stage)" in expr and '__error__=""' in expr


def test_the_events_hold_what_the_bars_read():
    from types import SimpleNamespace

    import events

    args = SimpleNamespace(environment=None, stage="Approval", result="ok", seconds=21433.7, deploy=False,
                           recovery_seconds=None, job=None)
    body = events.event(args, {"build": {"number": "120"}, "service": "orders-service", "version": "1.33.0", "jira_key": "ORD-140"})
    assert (body["build"], body["stage"], body["result"], body["duration_seconds"]) == ("120", "Approval", "ok", 21433.7)


def test_a_bar_rounds_up_as_slack_does_and_can_fall_back():
    assert all(dashboard.ROUNDED_UP in target["expr"] for target in BARS["targets"])
    plain = next(panel for panel in dashboard.build(plain=True)["panels"] if panel["type"] == "bargauge")
    assert all(dashboard.ROUNDED_UP not in target["expr"] and dashboard.AS_SENT in target["expr"] for target in plain["targets"])
    assert all(dashboard.ROUNDED_UP in target["expr"] for target in BARS["targets"]), "build() must not change the file's copy"


def test_one_scale_and_approval_in_its_own_colour():
    defaults = BARS["fieldConfig"]["defaults"]
    assert defaults["unit"] == "dthms" and defaults["min"] == 0 and "max" not in defaults
    override = BARS["fieldConfig"]["overrides"][0]
    approval = next(target for target in BARS["targets"] if target["legendFormat"] == "Approval")
    assert override["matcher"] == {"id": "byFrameRefID", "options": approval["refId"]}
    assert override["properties"][0]["value"]["fixedColor"] == "#ff9900" and defaults["color"]["fixedColor"] == "#10aced"


def test_nothing_on_it_says_dora_or_demo_system_or_a_dash():
    text = (HERE / "dashboard.json").read_text()
    for forbidden in ("DORA", "Demo system", chr(0x2013), chr(0x2014), "client"):
        assert forbidden not in text


def test_the_page_says_what_is_chosen():
    line = next(panel for panel in DASHBOARD["panels"] if panel["type"] == "text")["options"]["content"]
    assert "Every bar is measured" in line and "How long Approval takes is up to whoever plays the approver" in line


def test_the_address_names_the_run_and_nothing_else():
    assert dashboard.address("https://stack.example/", 120) == "https://stack.example/d/release-demo-release-wait?orgId=1&var-build=120"


class FakeSsm:
    pass


def test_url_prints_the_address_and_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(dashboard, "ssm_client", lambda: FakeSsm())
    monkeypatch.setattr(dashboard, "get_parameter", lambda ssm, name: {"grafana/stack-url": "https://stack.example"}[name])
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("--url must not write"))
    monkeypatch.setattr(sys, "argv", ["dashboard.py", "--url", "120"])
    assert dashboard.main() == 0
    assert capsys.readouterr().out.strip() == "https://stack.example/d/release-demo-release-wait?orgId=1&var-build=120"


def test_the_upload_goes_to_the_folder_with_the_token_it_is_given(monkeypatch, capsys):
    sent = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"status": "success", "version": 3, "url": "/d/release-demo-release-wait/where"}).encode()

    monkeypatch.setattr(dashboard, "ssm_client", lambda: FakeSsm())
    monkeypatch.setattr(dashboard, "get_parameter", lambda ssm, name: {"grafana/stack-url": "https://stack.example",
                                                                       "grafana/service-account-token": "not-a-real-token"}[name])
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", lambda request, timeout: sent.append(request) or Reply())
    monkeypatch.setattr(sys, "argv", ["dashboard.py"])
    assert dashboard.main() == 0
    assert sent[0].full_url == "https://stack.example/api/dashboards/db"
    body = json.loads(sent[0].data)
    assert body["folderUid"] == "release-demo-dashboards" and body["overwrite"] is True and body["dashboard"]["uid"] == dashboard.UID
    assert "not-a-real-token" not in capsys.readouterr().out
