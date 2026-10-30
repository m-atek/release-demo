"""Demo 21: the release train's decisions, the shared scripts' several Jira keys,
the flag in 1.8.0, the reset's AppConfig steps and the switch. Jira, Slack,
Jenkins and AppConfig are fakes.

    .venv/bin/python -m pytest -q scripts/demos/release-train/tests
"""

import datetime
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

import demo
import flags
import run_state
import setup
import traffic
import train

ROOT = Path(__file__).resolve().parents[4]
STATE = ROOT / "demo-states" / "orders-service" / "1.8.0"
DECLARED = {"delivery_estimate_v2": {"issue": "ORD-93", "default": False}}
FOUND = [{"key": "ORD-91", "summary": "Add the item count to each order in /api/orders", "status": "Done", "done": True},
         {"key": "ORD-92", "summary": "Show order totals with a thousands separator", "status": "Done", "done": True},
         {"key": "ORD-93", "summary": "Delivery estimate per carrier", "status": "In Progress", "done": False}]


def at(day, hour, minute):
    return datetime.datetime(2026, 11, day, hour, minute, 4)


def test_the_train_leaves_only_at_a_departure_on_a_weekday():
    assert train.departure(at(3, 14, 0), ["14:00"]) == "14:00"        # a Tuesday
    assert train.departure(at(3, 13, 30), ["14:00"]) is None
    assert train.departure(at(7, 14, 0), ["14:00"]) is None           # a Saturday
    assert train.departure(at(3, 10, 30), ["10:30", "14:00"]) == "10:30"


def test_an_unfinished_issue_rides_behind_a_flag_that_is_off():
    rows, stopped = train.rows_for(FOUND, DECLARED, {"delivery_estimate_v2": False})
    assert stopped is None
    assert rows == ["ORD-91 Add the item count to each order in /api/orders: done",
                    "ORD-92 Show order totals with a thousands separator: done",
                    "ORD-93 Delivery estimate per carrier: in progress, behind delivery_estimate_v2 (off)"]
    assert train.rows_for(FOUND, DECLARED, {})[1] is None  # missing in production means off


def test_an_unfinished_issue_without_a_flag_that_is_off_stops_the_train():
    assert train.rows_for(FOUND, DECLARED, {"delivery_estimate_v2": True})[1] == "ORD-93"
    assert train.rows_for(FOUND, {}, {})[1] == "ORD-93"


class Recorder:
    def __init__(self):
        self.posted, self.started = [], []


@pytest.fixture
def world(monkeypatch, tmp_path):
    recorder = Recorder()
    monkeypatch.setattr(train, "NOTE", tmp_path / "train.txt")
    monkeypatch.setattr(train, "slack_post", lambda text: recorder.posted.append(text) or
                        {"ok": True, "channel": "C0RELEASES", "ts": "1793000000.000100"})
    monkeypatch.setattr(train, "issues", lambda name, version: FOUND)
    monkeypatch.setattr(flags, "declared", lambda directory=None: DECLARED)
    monkeypatch.setattr(flags, "production_values", lambda environment="production": {"delivery_estimate_v2": False})

    class Job:
        def start(self, params):
            recorder.started.append(params)
            return 41

        def started(self, queue_id, timeout=1800, interval=3, waiting=None):
            return 212

    release_job = type(sys)("release_job")
    release_job.ReleaseJob = type("ReleaseJob", (), {"as_release_bot": staticmethod(lambda: Job())})
    monkeypatch.setitem(sys.modules, "release_job", release_job)
    config = train.service("orders-service")
    config["dir"] = STATE
    return recorder, config


def test_the_train_posts_its_message_then_starts_the_release_from_it(world, monkeypatch):
    recorder, config = world
    monkeypatch.setattr(train, "live_version", lambda name, config: "1.4.0")
    line = train.leave("orders-service", config, "the 14:00 release train", dry_run=False)
    assert line == "Released orders-service 1.8.0 for ORD-91, ORD-92, ORD-93: release #212."
    (message,) = recorder.posted
    assert message.startswith("*Release orders-service 1.8.0* · ORD-91, ORD-92, ORD-93 · requested by the 14:00 release train")
    assert "○  Build" in message and "○  Approval" in message
    assert "ORD-93 Delivery estimate per carrier: in progress, behind delivery_estimate_v2 (off)" in message
    (params,) = recorder.started
    assert params == {"SERVICE": "orders-service", "JIRA_KEY": "ORD-91,ORD-92,ORD-93", "OPTION": "train",
                      "REQUESTED_BY": "the 14:00 release train", "SLACK_CHANNEL": "C0RELEASES",
                      "SLACK_TS": "1793000000.000100", "REQUESTED_AT": "1793000000.000100",
                      "ROWS": json.dumps(train.rows_for(FOUND, DECLARED, {})[0])}


def test_nothing_new_means_no_train_today(world, monkeypatch):
    recorder, config = world
    monkeypatch.setattr(train, "live_version", lambda name, config: "1.8.0")
    assert train.leave("orders-service", config, "the 14:00 release train", dry_run=False) == (
        "No train today: nothing new since 1.8.0.")
    assert recorder.posted == ["No train today: nothing new since 1.8.0."] and recorder.started == []


def test_an_unfinished_issue_without_its_flag_off_stops_the_train(world, monkeypatch):
    recorder, config = world
    monkeypatch.setattr(train, "live_version", lambda name, config: "1.4.0")
    monkeypatch.setattr(flags, "production_values", lambda environment="production": {"delivery_estimate_v2": True})
    train.leave("orders-service", config, "the 14:00 release train", dry_run=False)
    assert recorder.posted == ["Train stopped: ORD-93 isn't done and isn't behind a flag that's off."]
    assert recorder.started == []


def test_a_timer_tick_that_isnt_a_departure_or_while_switched_off_leaves_nothing(world, monkeypatch, tmp_path):
    recorder, _ = world
    monkeypatch.setattr(sys, "argv", ["train.py", "--timer"])
    monkeypatch.setattr(train, "now_in_amsterdam", lambda: at(3, 10, 30))
    assert train.main() == train.NOT_DEPARTING
    assert (tmp_path / "train.txt").read_text().startswith("No departure at 10:30")
    monkeypatch.setattr(train, "now_in_amsterdam", lambda: at(3, 14, 0))
    monkeypatch.setattr(train, "get_parameter", lambda ssm, name: "off")
    monkeypatch.setattr(train, "ssm_client", lambda: None)
    assert train.main() == train.NOT_DEPARTING
    assert recorder.posted == [] and recorder.started == []


def test_a_rehearsal_says_so(world, monkeypatch):
    recorder, _ = world
    monkeypatch.setattr(train, "live_version", lambda name, config: "1.4.0")
    monkeypatch.setattr(train, "service", lambda name: {**train.services()[name], "name": name, "dir": STATE})
    monkeypatch.setattr(sys, "argv", ["train.py", "--rehearsal"])
    assert train.main() == 0
    assert "requested by a release train rehearsal" in recorder.posted[0]


# What the shared scripts do with several keys, and what they keep doing with one.

def test_run_state_keeps_the_keys_and_the_rows_for_slack_and_the_change(monkeypatch):
    rows = train.rows_for(FOUND, DECLARED, {})[0]
    monkeypatch.setattr(sys, "argv", ["run_state.py", "init", "--service", "orders-service", "--jira-key",
                                      "ORD-91,ORD-92,ORD-93", "--option", "train", "--requested-by",
                                      "the 14:00 release train", "--rows", json.dumps(rows)])
    run_state.main()
    state = run_state.load()
    assert state["jira_keys"] == ["ORD-91", "ORD-92", "ORD-93"]
    assert state["rows"] == rows and state["change_details"] == ["Issues in this release:", *rows]
    import notify

    assert notify.render(state).startswith(f"*Release orders-service {state['version']}* · ORD-91, ORD-92, ORD-93 · "
                                           "requested by the 14:00 release train")


def test_one_key_reads_as_before(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_state.py", "init", "--service", "orders-service", "--jira-key", "ORD-12",
                                      "--requested-by", "U0SAM000001"])
    run_state.main()
    state = run_state.load()
    assert state["jira_keys"] == ["ORD-12"] and "rows" not in state and "change_details" not in state
    import notify

    assert " · ORD-12 · requested by <@U0SAM000001>" in notify.render(state)


def test_the_trains_change_names_the_train_and_lists_the_issues():
    import change

    state = {"service": "orders-service", "version": "1.8.0", "option": "train", "jira_key": "ORD-91,ORD-92,ORD-93",
             "jira_keys": ["ORD-91", "ORD-92", "ORD-93"], "requested_by": "the 14:00 release train"}
    assert change.what_for(state) == "release train 14:00"
    assert change.what_for({**state, "requested_by": "a release train rehearsal"}) == "release train rehearsal"
    assert change.what_for({**state, "option": "", "jira_key": "ORD-12"}) == "ORD-12"
    assert change.issue_link("https://site", ["ORD-91", "ORD-92"]).startswith("https://site/issues/?jql=key%20in%20%28ORD-91")
    import yaml

    standard = yaml.safe_load((ROOT / "services.yaml").read_text())["orders-service"]["servicenow"]["standard_options"]
    assert "train" in standard and "standard" in standard


class FakeJira:
    def __init__(self):
        self.calls = []

    def version(self, name, create=True):
        return {"id": "10050", "name": name}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))

    def transition(self, key, target):
        self.calls.append(("transition", key, target))
        return True


@pytest.mark.parametrize("keys, moved", [("ORD-91,ORD-92,ORD-93", []), ("ORD-12", [("transition", "ORD-12", "Done")])])
def test_jira_comments_on_each_key_and_releases_the_version_once(monkeypatch, keys, moved):
    import jira

    fake = FakeJira()
    monkeypatch.setattr(jira, "Jira", lambda: fake)
    run_state.save({"service": "orders-service", "version": "1.8.0", "jira_key": keys, "jira_keys": keys.split(",")})
    for argv in (["comment", "Released."], ["release"]):
        monkeypatch.setattr(sys, "argv", ["jira.py", *argv])
        jira.main()
    comments = [path for method, path, _ in fake.calls if method == "POST"]
    assert comments == [f"/rest/api/3/issue/{key}/comment" for key in keys.split(",")]
    assert [c for c in fake.calls if c[0] == "PUT" and "/version/" in c[1]] == [
        ("PUT", "/rest/api/3/version/10050", {"released": True, "releaseDate": datetime.date.today().isoformat()})]
    assert [c for c in fake.calls if c[0] == "transition"] == moved


# 1.8.0's flag.

def load_handler(monkeypatch, enabled):
    spec = importlib.util.spec_from_file_location("handler_1_8_0", STATE / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_poll_flags", lambda: module._flags.update(
        values={"delivery_estimate_v2": {"enabled": enabled}}))
    monkeypatch.setenv("DEPLOYED_AT", "3 Nov 2026, 13:00 UTC")
    return module


def answer(module, path, store=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": "GET", "path": path}}}
    if store:
        event["queryStringParameters"] = {"store": store}
    try:
        return module.handler(event, None)
    except Exception:
        return {"statusCode": 502, "body": ""}


def test_with_the_flag_off_1_8_0_reads_like_1_4_0(monkeypatch):
    module = load_handler(monkeypatch, False)
    page = answer(module, "/")["body"]
    assert "Delivery estimate: 2 days</p>" in page and ">1.8.0<" in page
    assert all(answer(module, "/api/estimate", store)["statusCode"] == 404 for store in traffic.STORES)


def test_with_the_flag_on_one_store_fails_and_the_others_answer(monkeypatch):
    module = load_handler(monkeypatch, True)
    assert "Delivery estimate: 2 days with Polder Post</p>" in answer(module, "/")["body"]
    statuses = {store: answer(module, "/api/estimate", store)["statusCode"] for store in traffic.STORES}
    assert statuses == {**{store: 200 for store in traffic.STORES[:5]}, "Groningen Vismarkt": 502}


def test_the_flag_hides_the_unfinished_issue_and_carries_no_disclaimer():
    import yaml

    declared = yaml.safe_load((STATE / "flags.yaml").read_text())
    assert set(declared) == {"delivery_estimate_v2"} and declared["delivery_estimate_v2"]["default"] is False
    names = yaml.safe_load((ROOT / "services.yaml").read_text())["orders-service"]["flags"]["names"]
    assert names == list(declared)
    assert "Demo system" not in (STATE / "handler.py").read_text()
    assert not (STATE / "migrations").exists()  # 1.8.0 needs no database


def test_setup_writes_the_keys_where_the_demo_reads_them(tmp_path, monkeypatch):
    flags_file, calendar = tmp_path / "flags.yaml", tmp_path / "release-calendar.html"
    shutil.copy(STATE / "flags.yaml", flags_file)
    shutil.copy(ROOT / "demo-data" / "release-calendar.html", calendar)
    monkeypatch.setattr(setup, "FLAGS", flags_file)
    monkeypatch.setattr(setup, "CALENDAR", calendar)
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    setup.write_keys({"u": "ORD-91", "v": "ORD-92", "w": "ORD-93"})
    import yaml

    assert yaml.safe_load(flags_file.read_text())["delivery_estimate_v2"]["issue"] == "ORD-93"
    page = calendar.read_text()
    assert "<td>ORD-93</td><td>Delivery estimate per carrier</td><td class=\"late\">not ready</td>" in page
    setup.write_keys({"u": "ORD-91", "v": "ORD-92", "w": "ORD-93"})  # again: nothing to change
    assert calendar.read_text() == page


def test_the_calendar_keeps_its_footer_below_the_fold():
    page = (ROOT / "demo-data" / "release-calendar.html").read_text()
    assert page.index('<div class="spacer"></div>') < page.index("<footer>Fictional calendar, made for a demo.</footer>")
    assert ".spacer { height: 100vh; }" in page and "Release window" in page and "Change freeze" in page


# The reset's flag step, and the switch.

class FakeAppConfig:
    def __init__(self, items, content):
        self.items, self.content, self.stopped, self.created, self.started = items, content, [], [], []

    def list_deployments(self, ApplicationId, EnvironmentId):
        return {"Items": self.items[EnvironmentId]}

    def get_hosted_configuration_version(self, ApplicationId, ConfigurationProfileId, VersionNumber):
        return {"Content": io.BytesIO(json.dumps(self.content[VersionNumber]).encode())}

    def list_hosted_configuration_versions(self, ApplicationId, ConfigurationProfileId):
        return {"Items": [{"VersionNumber": n} for n in self.content]}

    def stop_deployment(self, ApplicationId, EnvironmentId, DeploymentNumber):
        self.stopped.append((EnvironmentId, DeploymentNumber))
        self.items[EnvironmentId] = [i for i in self.items[EnvironmentId] if i["DeploymentNumber"] != DeploymentNumber]

    def create_hosted_configuration_version(self, **kwargs):
        self.created.append(kwargs)
        return {"VersionNumber": max(self.content) + 1}

    def start_deployment(self, **kwargs):
        self.started.append(kwargs)
        return {"DeploymentNumber": 12}


KNOWN = {"application": "app1", "environments": {"staging": "env-s", "production": "env-p"}, "profile": "prof1",
         "strategy": "strat1", "reset_strategy": "strat0"}
ON, OFF = ({"version": "1", "values": {"delivery_estimate_v2": {"enabled": value}}} for value in (True, False))


def test_the_reset_stops_a_running_switch_and_deploys_off_only_where_it_is_on(monkeypatch):
    items = {"env-s": [{"State": "COMPLETE", "ConfigurationVersion": "1", "DeploymentNumber": 3}],
             "env-p": [{"State": "BAKING", "ConfigurationVersion": "3", "DeploymentNumber": 11},
                       {"State": "COMPLETE", "ConfigurationVersion": "2", "DeploymentNumber": 10}]}
    fake = FakeAppConfig(items, {1: OFF, 2: ON, 3: ON})
    monkeypatch.setattr(flags, "ids", lambda: KNOWN)
    monkeypatch.setattr(flags, "client", lambda name="appconfig": fake)
    spec = importlib.util.spec_from_file_location("train_reset", ROOT / "scripts" / "demos" / "release-train" / "reset.py")
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    actions = reset.shared_reset().Reset(dry_run=False)
    reset.flag_off(actions)
    assert fake.stopped == [("env-p", 11)]
    (started,) = fake.started
    assert started["EnvironmentId"] == "env-p" and started["DeploymentStrategyId"] == "strat0"
    assert started["Description"] == "reset.py before the next take"
    assert started["Tags"] == {"requested_by": "reset.py", "flag": "delivery_estimate_v2", "state": "off"}
    assert json.loads(fake.created[0]["Content"])["values"]["delivery_estimate_v2"] == {"enabled": False}


def test_the_reset_archives_only_what_this_demo_releases():
    spec = importlib.util.spec_from_file_location("train_reset2", ROOT / "scripts" / "demos" / "release-train" / "reset.py")
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    shared = reset.shared_reset()
    assert shared.after_baseline("1.8.0") and not shared.after_baseline("1.6.0") and not shared.after_baseline("1.4.1")


def test_the_schedule_starts_the_host_half_an_hour_before_the_first_departure():
    assert demo.start_expression("14:00") == ("cron(30 13 ? * MON-FRI *)", "13:30")
    assert demo.start_expression("10:15") == ("cron(45 9 ? * MON-FRI *)", "09:45")


def test_the_traffic_goes_round_the_stores_and_names_the_one_that_fails(monkeypatch):
    def answer_for(url, timeout=10):
        return (502 if "Groningen" in url else 200), ""

    monkeypatch.setattr(traffic, "http_get", answer_for)
    said = []
    results = traffic.run("https://production.example", minutes=0.02, rate=50, sink=None, out=said.append)
    assert {store for store, _ in results} == set(traffic.STORES)
    assert said[-1] == "failed stores: Groningen Vismarkt"


def test_the_jobs_and_the_trigger():
    jobs = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    assert "pipelineJob('release-train')" in jobs
    assert r"cron { spec('TZ=Europe/Amsterdam\n0,30 7-19 * * 1-5') }" in jobs
    assert "stringParam('ROWS', ''" in jobs
    jenkinsfile = (ROOT / "jenkins" / "jobs" / "release-train.Jenkinsfile").read_text()
    assert "--timer" in jenkinsfile and "currentBuild.result = 'NOT_BUILT'" in jenkinsfile
    pipeline = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
    assert '--rows "${ROWS:-}"' in pipeline
