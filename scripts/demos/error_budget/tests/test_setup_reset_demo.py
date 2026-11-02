"""setup.py, reset.py, demo.py and the dashboard, against fakes."""

import importlib.util
import json
from types import SimpleNamespace

import pytest

import budget
import dashboard
import demo
import setup as budget_setup
from conftest import HERE, NOW, FakeCloudWatch, FakeJira, budget_issue, story

DASHBOARD = dashboard.build()
PANELS = {panel.get("title") or panel["id"]: panel for panel in DASHBOARD["panels"]}
REQUESTS_PER_MINUTE = "IF(FILL(s,-1) >= 0, FILL(s,0), FILL(r,0))"
FAILED_PER_MINUTE = "IF(FILL(s,-1) >= 0, 0, FILL(f,0))"


def load_reset():
    spec = importlib.util.spec_from_file_location("budget_reset", HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# setup.py

def test_setup_makes_the_three_stories_once_and_prints_their_keys(capsys):
    jira = FakeJira(next_key=110)
    budget_setup.stories(jira)
    assert jira.created == ["ORD-110", "ORD-111", "ORD-112"]
    assert all(jira.issues[key]["type"] == "Story" and jira.issues[key]["reporter"] == "Release Pipeline" for key in jira.created)
    out = capsys.readouterr().out
    assert 'ORD-[p] is ORD-110: Add a price lookup to the API (made now)' in out
    assert "ORD-[a] is ORD-111: Accept product names from the old store app" in out
    assert "ORD-[f] is ORD-112: Show the pickup time on the order page" in out
    budget_setup.stories(jira)
    assert jira.created == ["ORD-110", "ORD-111", "ORD-112"] and "there already" in capsys.readouterr().out
    assert budget_setup.story_keys(jira) == {"p": "ORD-110", "a": "ORD-111", "f": "ORD-112"}


def test_setup_finds_only_the_exact_summary_and_a_dry_run_makes_nothing():
    jira = FakeJira([story("ORD-100", summary="Show the pickup time on the order page, and more")])
    assert budget_setup.find_story(jira, "Show the pickup time on the order page") is None
    budget_setup.stories(jira, dry_run=True)
    assert jira.created == []
    with pytest.raises(RuntimeError, match="run scripts/demos/error_budget/setup.py once"):
        budget_setup.story_keys(jira)


def test_the_stories_carry_no_budget_label():
    jira = FakeJira(next_key=110)
    budget_setup.stories(jira)
    assert all("error-budget" not in jira.issues[key]["labels"] for key in jira.created)


# reset.py

class ResetWorld:
    """The shared reset's steps recorded; Lambda, Jira, GitHub and SSM as the test sets them."""

    def __init__(self, monkeypatch, has_baseline=True, main="1.28.1", switches=None):
        self.reset = load_reset()
        self.calls = []
        self.switches = {"error-budget/enabled": "off", "ai-notes/enabled": "off", "cost-check/enabled": "off", **(switches or {})}
        shared = self.shared = self.reset.shared_reset()
        world = self
        monkeypatch.setattr(self.reset, "shared_reset", lambda: shared)
        for name in ("stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "lambda_aliases", "warm_slack_handler",
                     "jira_versions"):
            monkeypatch.setattr(shared, name, lambda *args, n=name: world.calls.append(n))
        monkeypatch.setattr(shared, "release_jobs", lambda: [])
        self.jira = FakeJira([budget_issue("ORD-123"), budget_issue("ORD-90", status="Done")])
        self.traffic = "2036-11-07T09:00:00+00:00"
        self.cloudwatch = FakeCloudWatch()
        monkeypatch.setattr(self.reset, "cloudwatch_client", lambda: world.cloudwatch)
        monkeypatch.setattr(self.reset, "stories", lambda: {"p": "ORD-110", "a": "ORD-111", "f": "ORD-112"})
        monkeypatch.setattr(self.reset, "has_baseline", lambda: has_baseline)
        monkeypatch.setattr(self.reset.host, "main_version", lambda path: main)
        monkeypatch.setattr(self.reset.host, "stage", lambda service, version: world.calls.append(f"stage {version}"))
        monkeypatch.setattr(self.reset, "release_baseline", lambda reset, key: world.calls.append(f"release under {key}"))
        original = self.reset.close_budget_issues
        monkeypatch.setattr(self.reset, "close_budget_issues", lambda reset: original(reset, world.jira))

        class Ssm:
            class exceptions:
                class ParameterNotFound(Exception):
                    pass

            def get_parameter(self, Name):
                name = Name.removeprefix("/release-demo/")
                if name == "error-budget/traffic-until":
                    return {"Parameter": {"Value": world.traffic}}
                return {"Parameter": {"Value": world.switches[name]}}

            def put_parameter(self, Name, Value, Type, Overwrite):
                assert Type == "String" and Overwrite
                world.switches[Name.removeprefix("/release-demo/")] = Value

        monkeypatch.setattr(self.reset, "ssm_client", lambda: Ssm())


def test_the_reset_points_the_shared_steps_at_this_videos_versions_only():
    shared = load_reset().shared_reset()
    assert shared.BASELINE == "1.26.0"
    assert all(shared.after_baseline(version) for version in ("1.27.0", "1.27.1", "1.28.0", "1.28.1"))
    for version in ("1.26.0", "1.4.0", "1.8.0", "1.12.0", "1.29.0"):
        assert not shared.after_baseline(version), version


def test_a_reset_after_the_first_time_frees_the_takes_and_turns_the_check_on(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    assert world.reset.run(False, "https://handler.example") == 0
    assert world.calls == ["stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "jira_versions", "stage 1.27.0",
                           "lambda_aliases", "warm_slack_handler"]
    assert world.shared.DEMO_ISSUES == ["ORD-111", "ORD-112"]
    assert world.switches == {"error-budget/enabled": "on", "ai-notes/enabled": "off", "cost-check/enabled": "off"}
    assert world.jira.issues["ORD-123"]["status"] == "Done" and world.jira.comments == [("ORD-123", "Earlier take")]
    assert world.jira.issues["ORD-90"]["status"] == "Done" and "ORD-90" not in [key for key, _ in world.jira.moved]
    assert "Ready for a take." in capsys.readouterr().out


def test_the_reset_is_safe_to_run_twice(monkeypatch, capsys):
    world = ResetWorld(monkeypatch, main="1.27.0", switches={"error-budget/enabled": "on"})
    world.jira = FakeJira()
    assert world.reset.run(False, None) == 0
    out = capsys.readouterr().out
    # The one action left is the seed, which changes nothing it wrote before.
    assert "stage 1.27.0" not in world.calls and "main has orders-service 1.27.0" in out and "1 action(s)" in out


def test_the_reset_turns_the_other_switches_off(monkeypatch):
    world = ResetWorld(monkeypatch, switches={"ai-notes/enabled": "on", "cost-check/enabled": "on"})
    assert world.reset.run(False, None) == 0
    assert world.switches == {"error-budget/enabled": "on", "ai-notes/enabled": "off", "cost-check/enabled": "off"}


def test_a_dry_run_changes_nothing(monkeypatch, capsys):
    world = ResetWorld(monkeypatch, switches={"ai-notes/enabled": "on"})
    assert world.reset.run(True, None) == 0
    out = capsys.readouterr().out
    assert "would turn error-budget/enabled on" in out and "would stage orders-service 1.27.0 on main" in out
    assert "would move ORD-123 to Done" in out
    assert world.switches["error-budget/enabled"] == "off" and world.switches["ai-notes/enabled"] == "on"
    assert world.jira.comments == [] and "stage 1.27.0" not in world.calls
    assert "would seed 1,437 minutes" in out and world.cloudwatch.written == []


def test_the_reset_seeds_last_so_every_request_after_it_is_real(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    order = []
    monkeypatch.setattr(world.shared, "warm_slack_handler", lambda *args: order.append("warmed"))
    real_write = world.reset.load_seed
    monkeypatch.setattr(world.reset, "load_seed", lambda: (order.append("seed loaded"), real_write())[1])
    assert world.reset.run(False, None) == 0
    assert order == ["warmed", "seed loaded"]
    assert sum(len(points) for _, points in world.cloudwatch.written) == 1437
    assert {namespace for namespace, _ in world.cloudwatch.written} == {"ReleaseDemo/ErrorBudget"}
    assert "seed 1,437 minutes" in capsys.readouterr().out


def test_the_reset_won_t_seed_while_the_traffic_is_off(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    world.traffic = "off"
    assert world.reset.run(False, None) == 1
    out = capsys.readouterr().out
    assert "the traffic is off" in out and "scripts/demo.sh up error-budget" in out and "these steps failed: 11." in out
    assert world.cloudwatch.written == []


def test_with_no_seed_setting_the_reset_seeds_nothing_and_the_page_still_builds(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    real = world.reset.service

    def without_seed(name):
        config = real(name)
        config["error_budget"].pop("seed")
        return config

    monkeypatch.setattr(world.reset, "service", without_seed)
    world.traffic = "off"  # a real fill doesn't ask: the take's own traffic is the caller's
    assert world.reset.run(False, None) == 0
    assert world.cloudwatch.written == [] and "names no seed" in capsys.readouterr().out
    monkeypatch.setattr(dashboard, "service", without_seed)
    assert dashboard.build()["uid"] == "release-demo-error-budget"


def test_a_dry_run_only_says_the_traffic_is_off(monkeypatch, capsys):
    world = ResetWorld(monkeypatch)
    world.traffic = "2020-01-01T00:00:00+00:00"
    assert world.reset.run(True, None) == 0
    assert "the traffic is off" in capsys.readouterr().out and world.cloudwatch.written == []


def test_the_first_time_is_a_real_release_under_the_price_story(monkeypatch):
    world = ResetWorld(monkeypatch, has_baseline=False)
    assert world.reset.run(False, None) == 0
    assert world.calls.index("release under ORD-110") < world.calls.index("stage 1.27.0") < world.calls.index("lambda_aliases")


def test_a_failed_first_release_stages_nothing_and_turns_nothing_on(monkeypatch, capsys):
    world = ResetWorld(monkeypatch, has_baseline=False)

    def broken(reset, key):
        raise SystemExit("The release of 1.26.0, build #70, ended FAILURE.")

    monkeypatch.setattr(world.reset, "release_baseline", broken)
    assert world.reset.run(False, None) == 1
    assert "stage 1.27.0" not in world.calls and world.switches["error-budget/enabled"] == "off"
    assert "Stopped:" in capsys.readouterr().out


def test_the_first_release_runs_with_every_switch_off(monkeypatch):
    world = ResetWorld(monkeypatch, has_baseline=False, switches={"error-budget/enabled": "on", "ai-notes/enabled": "on"})
    reset = load_reset()
    reset_object = world.shared.Reset(True)
    monkeypatch.setattr(reset, "ssm_client", world.reset.ssm_client)

    seen = []
    monkeypatch.setattr(reset, "set_switches", lambda r, wanted: seen.append(wanted))
    reset.release_baseline(reset_object, "ORD-110")
    assert seen == [{"error-budget/enabled": "off", "ai-notes/enabled": "off", "cost-check/enabled": "off"}]


# demo.py

class FakeLambda:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, exists=True):
        self.exists = exists

    def get_function_configuration(self, FunctionName, Qualifier=None):
        if not self.exists:
            raise self.exceptions.ResourceNotFoundException()
        return {"Description": "1.26.0"}

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": "70"}


def test_up_sets_the_traffic_for_72_hours_and_touches_nothing_else(monkeypatch, capsys):
    written = []
    monkeypatch.setattr(demo, "aws_session", lambda: SimpleNamespace(client=lambda name: FakeLambda()))
    monkeypatch.setattr(demo, "ssm_client", lambda: "ssm")
    monkeypatch.setattr(demo, "put_parameter", lambda ssm, name, value, secure=True: written.append((name, value, secure)))
    assert demo.up(now=NOW) == 0
    assert written == [("error-budget/traffic-until", "2026-11-07T12:47:20+00:00", False)]
    assert "seed.py" in capsys.readouterr().out


def test_up_applies_nothing_and_says_whose_apply_it_is(monkeypatch, capsys):
    written = []
    monkeypatch.setattr(demo, "aws_session", lambda: SimpleNamespace(client=lambda name: FakeLambda(exists=False)))
    monkeypatch.setattr(demo, "put_parameter", lambda *args, **kwargs: written.append(args))
    assert demo.up(now=NOW) == 1 and written == []
    assert "tf.sh apply" in capsys.readouterr().out


def test_down_turns_off_both_switches(monkeypatch):
    written = []

    class Switch:
        def set(self, value):
            written.append(("error-budget/enabled", value))

    monkeypatch.setattr(demo, "ssm_client", lambda: "ssm")
    monkeypatch.setattr(demo, "put_parameter", lambda ssm, name, value, secure=True: written.append((name, value)))
    monkeypatch.setattr(demo, "load_switch", lambda: Switch())
    assert demo.down() == 0
    assert written == [("error-budget/traffic-until", "off"), ("error-budget/enabled", "off")]


class FakeCheckSession:
    """What check reads: the function, its rule, the two policies and CloudWatch."""

    def __init__(self, rows):
        self.rows = rows

    def client(self, name):
        from conftest import FakeCloudWatch

        if name == "cloudwatch":
            cloudwatch = FakeCloudWatch(self.rows)
            cloudwatch.list_metrics = lambda **kwargs: {"Metrics": []}
            return cloudwatch
        if name == "lambda":
            return FakeLambda()
        if name == "events":
            return SimpleNamespace(describe_rule=lambda Name: {"State": "ENABLED", "ScheduleExpression": "rate(1 minute)"})
        return SimpleNamespace(get_role_policy=lambda RoleName, PolicyName: {"PolicyDocument": {
            "Statement": [{"Action": "cloudwatch:GetMetricData"}, {"Action": "ssm:PutParameter"},
                          {"Action": "cloudwatch:PutMetricData"}]}})


def test_check_reads_everything_the_demo_needs_and_says_ok_or_fail(monkeypatch, capsys):
    from conftest import window

    monkeypatch.setattr(demo, "plain_parameter", lambda name: "jenkins" if name.startswith("ci/") else "2026-11-07T09:00:00+00:00")
    monkeypatch.setattr(demo, "main_version", lambda path: "1.27.0")
    monkeypatch.setattr(demo, "load_switch", lambda: SimpleNamespace(value=lambda: "off"))
    as_of = budget.as_of_minute(NOW)
    found = demo.checks(FakeCheckSession(window(as_of)), now=NOW)
    text = "\n".join(what for _, what in found)
    assert all(ok for ok, _ in found), found
    assert "function release-demo-error-budget-traffic" in text and "rate(1 minute)" in text
    assert "last whole minute: 60 requests" in text and "0 of 86,400 failed" in text
    assert "Url5xxCount on orders-service-production: not published; services.yaml reads Errors" in text
    assert "live in production: 1.26.0; staged on main: 1.27.0" in text
    assert "/release orders-service starts on jenkins" in text and "Seeded: nothing in this window." in text
    assert "release-demo-tools-host may cloudwatch:PutMetricData" in text

    monkeypatch.setattr(demo, "plain_parameter", lambda name: "azure-pipelines" if name.startswith("ci/") else "off")
    found = dict((what, ok) for ok, what in demo.checks(FakeCheckSession(window(as_of, per_minute=40)), now=NOW))
    assert found["traffic off (error-budget/traffic-until: off)"] is False
    assert found["last whole minute: 40 requests (60, give or take 2), 0 failed"] is False
    assert found["/release orders-service starts on azure-pipelines: that demo's down first"] is False


def test_status_needs_no_secret(monkeypatch):
    # The switch's and the traffic's values are plain Strings, and main's VERSION comes from GitHub's public API.
    text = (HERE / "demo.py").read_text()
    assert "get_parameter(" not in text.replace(".get_parameter(", "") and "main_version(config" in text
    assert "github_app_token" not in text and "host.main_version" not in text


def test_the_wrappers_exist_and_run_the_right_action():
    for action in ("up", "down", "status", "check"):
        text = (HERE / f"{action}.sh").read_text()
        assert f'demos/error_budget/demo.py" {action}' in text
        assert (HERE / f"{action}.sh").stat().st_mode & 0o111
    assert (HERE / "tf.sh").stat().st_mode & 0o111


# The dashboard.

def targets(title):
    return {target["refId"]: target for target in PANELS[title]["targets"]}


def test_the_dashboard_is_where_and_what_the_spec_says():
    assert DASHBOARD["uid"] == "release-demo-error-budget" and DASHBOARD["title"] == "Error budget"
    assert DASHBOARD["time"] == {"from": "now-24h", "to": "now"}
    assert dashboard.FOLDER_UID == "release-demo-dashboards" and budget.DASHBOARD_UID == DASHBOARD["uid"]
    for title in ("Failed requests, running total", "Burn rate, last hour", "Failed, as the load generator saw them", "Overrides, last 14 days"):
        assert title in PANELS


def test_nothing_on_it_says_dora_or_demo_system_and_every_panel_gives_its_formula():
    text = json.dumps(DASHBOARD)
    assert "DORA" not in text and "Demo system" not in text and "description_per_minute" not in text
    for panel in DASHBOARD["panels"]:
        if panel["type"] != "text":
            assert len(panel["description"]) > 80, panel["title"]


def test_the_four_numbers_are_one_row_with_the_stages_arithmetic():
    four = PANELS[2]
    assert four["type"] == "stat" and four["gridPos"]["w"] == 24 and four["options"]["orientation"] == "horizontal"
    by_ref = {target["refId"]: target for target in four["targets"]}
    assert by_ref["A"]["metricName"] == "UrlRequestCount" and by_ref["B"]["metricName"] == "Errors"
    assert by_ref["G"]["namespace"] == "ReleaseDemo/ErrorBudget" and by_ref["G"]["metricName"] == "SeededRequests"
    assert by_ref["G"]["statistic"] == "Maximum" and by_ref["A"]["statistic"] == by_ref["B"]["statistic"] == "Sum"
    assert all(by_ref[ref]["hide"] for ref in "ABGDE") and not by_ref["R"]["hide"]
    assert by_ref["D"]["expression"] == REQUESTS_PER_MINUTE and by_ref["E"]["expression"] == FAILED_PER_MINUTE
    assert by_ref["R"]["expression"] == "TIME_SERIES(SUM(req))" and by_ref["F"]["expression"] == "TIME_SERIES(SUM(bad))"
    assert by_ref["L"]["expression"] == "FLOOR(TIME_SERIES(SUM(req)/1000))"
    assert by_ref["S"]["expression"] == "FLOOR(TIME_SERIES(SUM(bad)*100000/SUM(req)))"
    # Grafana sends a hidden query only if it has an id, and a math query needs the code editor's mode.
    assert all(target["id"] for target in four["targets"])
    assert all(target["metricEditorMode"] == (1 if target["expression"] else 0) for target in four["targets"])
    names = {o["matcher"]["options"]: o["properties"][0]["value"] for o in four["fieldConfig"]["overrides"]}
    assert names == {"R": "Requests", "F": "Failed", "L": "Allowed to fail: 0.1%", "S": "Budget spent"}
    # The visible order, as the script's frames read them: Requests, Failed, then the other two.
    assert [t["refId"] for t in four["targets"] if not t["hide"]] == ["R", "F", "L", "S"]


def minute_by_minute(seeded, real, failed):
    """The two per-minute expressions, as CloudWatch answered them when read for real on 2 November."""
    return [(seeded[m] if m in seeded else real.get(m, 0), 0 if m in seeded else failed.get(m, 0)) for m in sorted({*seeded, *real, *failed})]


def test_the_panels_read_each_minute_as_the_stage_does():
    seeded = {0: 60, 60: 61}
    real = {0: 7, 60: 7, 120: 60, 180: 60}
    failed = {0: 2, 120: 3, 180: 3}
    counts = {"seeded": seeded, "requests": real, "failed": failed}
    assert [row[1:] for row in budget.merged(counts, 0, 240)] == minute_by_minute(seeded, real, failed) == [(60, 0), (61, 0), (60, 3), (60, 3)]


def test_the_grafana_formulas_equal_the_stage_to_the_request():
    import math

    for requests in range(86_000, 87_000, 7):
        assert math.floor(requests / 1000) == budget.allowed_of(requests, 99.9)
        for failed in (0, 86, 87, 104, 140):
            assert math.floor(failed * 100000 / requests) == budget.spent_of(failed, requests, 99.9)


def test_the_chart_is_a_running_total_of_the_last_3_hours_with_the_dashed_line_and_the_marks():
    chart = PANELS["Failed requests, running total"]
    assert chart["timeFrom"] == "3h" and chart["hideTimeOverride"] is True
    assert chart["transformations"] == []
    by_ref = targets("Failed requests, running total")
    assert by_ref["E"]["expression"] == FAILED_PER_MINUTE and by_ref["E"]["hide"]
    assert by_ref["A"]["expression"] == "RUNNING_SUM(bad)" and not by_ref["A"]["hide"]
    assert by_ref["C"]["expression"] == "TIME_SERIES(86)" and not by_ref["C"]["hide"]
    override = chart["fieldConfig"]["overrides"][0]
    assert {"id": "displayName", "value": "budget: 86"} in override["properties"]
    assert {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}} in override["properties"]
    marks = [entry for entry in DASHBOARD["annotations"]["list"] if entry.get("target")]
    assert [mark["target"]["tags"] for mark in marks] == [["release-demo", "release", "orders-service"],
                                                          ["release-demo", "rollback", "orders-service"],
                                                          ["release-demo", "override", "orders-service"]]
    assert all(mark["filter"]["ids"] == [chart["id"]] for mark in marks)


def test_the_marks_tags_match_what_the_pipeline_sends():
    # events.py annotate adds release-demo in front of the tags it gets.
    pipeline = (HERE.parents[2] / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
    assert '--tags "release,$SERVICE"' in pipeline and '--tags "rollback,$SERVICE"' in pipeline
    assert "'error-budget', OVERRIDE, state['service']" in (HERE / "budget.py").read_text()


def test_the_fixed_ranges_and_the_loki_queries():
    assert PANELS["Burn rate, last hour"]["timeFrom"] == "1h" and PANELS["Overrides, last 14 days"]["timeFrom"] == "14d"
    assert "timeFrom" not in PANELS["Failed, as the load generator saw them"] and "timeFrom" not in PANELS[2]
    assert targets("Burn rate, last hour")["X"]["expression"] == "TIME_SERIES((SUM(bad)/SUM(req))*1000)"
    generator = targets("Failed, as the load generator saw them")["A"]["expr"]
    assert 'source="release-demo-loadgen"' in generator and 'kind="traffic"' in generator and "unwrap failed" in generator
    overrides = targets("Overrides, last 14 days")["A"]["expr"]
    assert 'kind="error_budget"' in overrides and 'result="override-approved"' in overrides


def test_the_budget_line_is_a_setting_for_the_partly_filled_fallback():
    built = dashboard.build(budget_line=19)
    chart = next(panel for panel in built["panels"] if panel.get("title") == dashboard.CHART)
    assert next(t for t in chart["targets"] if t["refId"] == "C")["expression"] == "TIME_SERIES(19)"
    assert {"id": "displayName", "value": "budget: 19"} in chart["fieldConfig"]["overrides"][0]["properties"]


def test_bars_per_minute_are_the_fallback_if_a_running_total_wont_draw():
    built = dashboard.build(per_minute=True)
    chart = next(panel for panel in built["panels"] if panel["id"] == 3)
    assert chart["title"] == "Failed requests per minute"
    assert next(t for t in chart["targets"] if t["refId"] == "A")["expression"] == "bad"
    assert chart["fieldConfig"]["defaults"]["custom"]["drawStyle"] == "bars"
    assert "DORA" not in json.dumps(built)


def test_the_metrics_names_follow_services_yaml():
    built = dashboard.build(metrics={"requests": "Invocations", "failed": "Url5xxCount"})
    names = {target["metricName"] for panel in built["panels"] for target in panel.get("targets", []) if target.get("metricName")}
    assert names == {"Invocations", "Url5xxCount", "SeededRequests"}
    # The file on disk holds what services.yaml says today, so a reader of the JSON isn't misled.
    plain = json.loads((HERE / "dashboard.json").read_text())
    assert {t["metricName"] for p in plain["panels"] for t in p.get("targets", []) if t.get("metricName")} == {
        "UrlRequestCount", "Errors", "SeededRequests"}


def test_nothing_on_the_page_says_the_fill_was_measured():
    line = PANELS[1]["options"]["content"]
    assert "seeded" in line and "CloudWatch counts" in line and "The requests come from a load generator" not in line
    assert len(line) < 210  # two lines at 125% in a half-width window, so the row sits where the card says
    for panel in (DASHBOARD, PANELS[2], PANELS["Failed requests, running total"]):
        assert "seeded" in panel["description"]
