"""Tests for scripts/grafana_setup.py and the dashboards in grafana/, without Grafana or AWS."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import grafana_setup  # noqa: E402

DASHBOARDS = sorted((ROOT / "grafana").glob("*.json"))
LONG_DASHES = "[" + chr(0x2013) + chr(0x2014) + "]"  # en and em dash, never used here


def load(path):
    return json.loads(path.read_text())


def panels(dashboard):
    for panel in dashboard["panels"]:
        yield panel
        yield from panel.get("panels", [])


def loki_queries(dashboard):
    return [target["expr"] for panel in panels(dashboard) for target in panel.get("targets", [])
            if target["datasource"]["type"] == "loki"]


class FakeGrafana:
    """Answers from a dict of (method, path) to (status, body), and records every call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.url = "https://example.grafana.net"

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.answers.get((method, path), (404, {"message": "not found"}))

    def must(self, method, path, body=None, ok=(200,)):
        return self.call(method, path, body)


def test_there_is_a_dashboard():
    assert [path.name for path in DASHBOARDS] == ["releases.json"]


def test_dashboards_never_name_dora():
    # Viewers would confuse it with the EU regulation: the panels name the measures.
    for path in DASHBOARDS:
        assert "dora" not in path.read_text().lower(), path.name


def test_dashboards_carry_the_demo_label():
    for path in DASHBOARDS:
        texts = [panel["options"]["content"] for panel in panels(load(path)) if panel["type"] == "text"]
        assert any("Demo system, not a client's." in text for text in texts), path.name


def test_files_have_no_long_dashes():
    for path in [*DASHBOARDS, ROOT / "scripts" / "grafana_setup.py", Path(__file__)]:
        assert not re.search(LONG_DASHES, path.read_text()), path.name


def test_the_change_record_links_the_releases_dashboard():
    # scripts/change.py links <stack>/d/release-demo from every change.
    assert load(ROOT / "grafana" / "releases.json")["uid"] == "release-demo"
    assert '"/d/release-demo"' in (ROOT / "scripts" / "change.py").read_text()


def test_panel_ids_are_unique_and_annotations_mark_existing_panels():
    dashboard = load(ROOT / "grafana" / "releases.json")
    ids = [panel["id"] for panel in panels(dashboard)]
    assert len(ids) == len(set(ids))
    for annotation in dashboard["annotations"]["list"]:
        assert set(annotation.get("filter", {}).get("ids", [])) <= set(ids)


def test_marks_follow_the_tags_events_py_sends():
    dashboard = load(ROOT / "grafana" / "releases.json")
    marks = [a for a in dashboard["annotations"]["list"] if a.get("target", {}).get("type") == "tags"]
    tags = sorted(tuple(a["target"]["tags"]) for a in marks)
    assert tags == [("release-demo", "release", "orders-service"), ("release-demo", "rollback", "orders-service")]
    pipeline = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
    assert '--tags "release,$SERVICE"' in pipeline and '--tags "rollback,$SERVICE"' in pipeline


def test_queries_read_only_release_events_with_fields_the_pipeline_sends():
    dashboard = load(ROOT / "grafana" / "releases.json")
    events = (ROOT / "scripts" / "events.py").read_text()
    pipeline = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
    for expr in loki_queries(dashboard):
        selectors = re.findall(r"\{source=[^}]*\}", expr)
        assert selectors and all('source="release-demo", service="orders-service"' in s for s in selectors), expr
        for field in re.findall(r"unwrap (\w+)", expr):
            assert f'"{field}"' in events, field
        for stage in re.findall(r'stage="([^"]+)"', expr):
            assert f"'{stage}'" in pipeline or f"--stage {stage} " in pipeline, stage


def test_comparable_ignores_what_grafana_adds():
    saved = {"uid": "release-demo", "title": "Releases", "id": 42, "version": 7}
    assert grafana_setup.comparable(saved) == grafana_setup.comparable({"uid": "release-demo", "title": "Releases"})


def test_an_unchanged_dashboard_is_not_saved_again():
    dashboard = load(ROOT / "grafana" / "releases.json")
    grafana = FakeGrafana({("GET", "/api/dashboards/uid/release-demo"): (
        200, {"dashboard": {**dashboard, "id": 3, "version": 4}, "meta": {"folderUid": grafana_setup.FOLDER["uid"]}})})
    grafana_setup.ensure_dashboard(grafana, dashboard)
    assert [call[0] for call in grafana.calls] == ["GET"]


def test_a_changed_dashboard_is_saved_into_the_folder():
    dashboard = load(ROOT / "grafana" / "releases.json")
    grafana = FakeGrafana({
        ("GET", "/api/dashboards/uid/release-demo"): (200, {"dashboard": {**dashboard, "title": "Old"}, "meta": {}}),
        ("POST", "/api/dashboards/db"): (200, {"version": 5}),
    })
    grafana_setup.ensure_dashboard(grafana, dashboard)
    method, path, body = grafana.calls[-1]
    assert (method, path) == ("POST", "/api/dashboards/db")
    assert body["folderUid"] == grafana_setup.FOLDER["uid"] and body["overwrite"] and body["dashboard"]["id"] is None


def test_the_folder_is_made_once():
    folder = grafana_setup.FOLDER
    there = FakeGrafana({("GET", "/api/folders?limit=1000"): (200, [{"uid": folder["uid"]}])})
    grafana_setup.ensure_folder(there)
    assert len(there.calls) == 1
    missing = FakeGrafana({("GET", "/api/folders?limit=1000"): (200, []), ("POST", "/api/folders"): (200, folder)})
    grafana_setup.ensure_folder(missing)
    assert missing.calls[-1] == ("POST", "/api/folders", folder)


def test_cloudwatch_waits_for_its_role(monkeypatch, capsys):
    monkeypatch.setattr(grafana_setup, "cloudwatch_role_arn", lambda: None)
    grafana = FakeGrafana({})
    assert grafana_setup.ensure_cloudwatch(grafana) is False
    assert grafana.calls == []
    assert "grafana_aws_account_id" in capsys.readouterr().out


def test_cloudwatch_uses_grafana_assume_role_with_the_stack_external_id(monkeypatch):
    arn = "arn:aws:iam::123456789012:role/release-demo-grafana-cloudwatch"
    monkeypatch.setattr(grafana_setup, "cloudwatch_role_arn", lambda: arn)
    grafana = FakeGrafana({("POST", "/api/datasources"): (200, {}),
                           ("GET", "/api/datasources/uid/release-demo-cloudwatch/health"): (200, {"status": "OK", "message": "ok"})})
    assert grafana_setup.ensure_cloudwatch(grafana) is True
    body = next(call[2] for call in grafana.calls if call[0] == "POST")
    assert body["jsonData"] == {"authType": "grafana_assume_role", "assumeRoleArn": arn, "defaultRegion": "eu-west-1",
                                "usePerDatasourceExternalId": False}


def test_cloudwatch_without_admin_rights_says_which_token_to_make(monkeypatch, capsys):
    monkeypatch.setattr(grafana_setup, "cloudwatch_role_arn", lambda: "arn:aws:iam::1:role/r")
    grafana = FakeGrafana({("POST", "/api/datasources"): (403, {"message": "permissions needed"})})
    assert grafana_setup.ensure_cloudwatch(grafana) is False
    out = capsys.readouterr().out
    assert "role Admin" in out and "secrets.py set grafana/setup-token" in out


def test_an_unchanged_cloudwatch_data_source_is_left_alone():
    wanted = grafana_setup.cloudwatch_body("arn:aws:iam::1:role/r")
    saved = {**wanted, "id": 9, "jsonData": {**wanted["jsonData"], "extra": "kept"}}
    assert grafana_setup.datasource_unchanged(saved, wanted)
    assert not grafana_setup.datasource_unchanged({**saved, "jsonData": {}}, wanted)


def test_the_cloudwatch_variable_prefers_its_saved_choice():
    dashboard = load(ROOT / "grafana" / "releases.json")
    ours = {"uid": "release-demo-cloudwatch", "type": "cloudwatch", "name": "CloudWatch (release-demo)", "jsonData": {"a": 1}}
    other = {"uid": "abc", "type": "cloudwatch", "name": "cloudwatch", "jsonData": {}}
    loki = {"uid": "grafanacloud-logs", "type": "loki", "name": "logs", "jsonData": {}}
    assert grafana_setup.resolve_datasources(dashboard, [loki, other, ours])["cloudwatch"] == ours
    assert grafana_setup.resolve_datasources(dashboard, [loki, other])["cloudwatch"] == other
    assert grafana_setup.resolve_datasources(dashboard, [loki])["cloudwatch"] is None


def test_summary_describes_logs_and_numbers():
    logs = {"schema": {"fields": [{"name": "labels"}, {"name": "Time"}, {"name": "Line", "type": "string"}]},
            "data": {"values": [[{}], [1], ["1.4.0"]]}}
    numbers = {"schema": {"fields": [{"name": "Time", "type": "time"}, {"name": "Value", "type": "number"}]},
               "data": {"values": [[1, 2], [0.5, 0.6666]]}}
    assert grafana_setup.summary([logs]) == "1 log lines, newest: 1.4.0"
    assert grafana_setup.summary([numbers]) == "0.67"
    assert grafana_setup.summary([]) == ""
