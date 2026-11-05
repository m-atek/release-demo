"""What video 29 adds to shared files does nothing for anything else; the pipeline, the job, Terraform and the reset's rules."""

import json
import re
from pathlib import Path

import pytest
import yaml

import events
import notify

ROOT = Path(__file__).resolve().parents[4]
TF = ROOT / "infra" / "demos" / "29-one-setting"
PIPELINE = (ROOT / "jenkins" / "shared-library" / "vars" / "settingPipeline.groovy").read_text()
SERVICES = yaml.safe_load((ROOT / "services.yaml").read_text())
ORDERS = SERVICES["orders-service"]


def release_state():
    return {"service": "orders-service", "version": "1.4.0", "jira_key": "ORD-12", "jira_keys": ["ORD-12"],
            "requested_by": "U0SAM000001", "build": {"number": "7"},
            "stages": {"Build": {"status": "waiting"}}, "lines": []}


def test_a_release_reads_as_it_always_has():
    assert notify.render(release_state()).startswith("*Release orders-service 1.4.0* · ORD-12 · requested by <@U0SAM000001>")
    args = type("Args", (), dict(stage="Build", result="ok", seconds=1.0, environment=None, deploy=False, recovery_seconds=None, job=None))()
    assert events.event(args, release_state())["service"] == "orders-service"


def test_no_other_service_has_settings():
    assert [name for name, config in SERVICES.items() if config.get("app_settings")] == ["orders-service"]


def test_the_release_pipeline_doesnt_know_settings():
    text = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
    assert "setting" not in text.lower()


def test_the_pipeline_builds_nothing_and_publishes_nothing():
    code = "\n".join(line for line in PIPELINE.splitlines() if not line.strip().startswith("//"))
    for word in ("toolchain", "publish", "jira.py", "--deploy", "deploy: true", "tag.py"):
        assert word not in code
    assert re.findall(r"releaseStage\('([^']+)'", PIPELINE) == ["AWS validator", "Staging", "Check staging", "Change", "Production",
                                                                 "Check production", "Record"]
    assert "releaseStage.skip('Approval'" in PIPELINE


def test_values_from_outside_never_reach_a_script_quoted():
    assert '"$VALUE"' not in PIPELINE and "${params.VALUE}" not in PIPELINE
    assert "${params.SETTING}" in PIPELINE.split("try {")[0]  # the build's name only: Slack's handler checked it
    assert "os.environ[\"VALUE\"]" in (ROOT / "scripts" / "demos" / "settings" / "setting.py").read_text()


def test_a_stop_before_production_cancels_and_after_closes_unsuccessful():
    stopped = PIPELINE.split("void stopped(error)")[1]
    assert "notify.py finish failed" in stopped and "change.py close unsuccessful" in stopped and "change.py cancel" in stopped
    assert "state('deploys.production.number')" in stopped


def test_the_job_exists_and_runs_one_at_a_time():
    text = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    job = text.split("pipelineJob('setting')")[1].split("\n}\n")[0]
    assert "disableConcurrentBuilds()" in job and "fromRepository('jenkins/setting.Jenkinsfile')" in job
    for name in ("SERVICE", "SETTING", "VALUE", "REQUESTED_BY", "SLACK_CHANNEL", "SLACK_TS", "REQUESTED_AT"):
        assert f"stringParam('{name}'" in job
    assert "settingPipeline()" in (ROOT / "jenkins" / "setting.Jenkinsfile").read_text()


def test_the_template_exists_with_its_texts_and_says_no_demo_system():
    servicenow = ORDERS["servicenow"]
    assert servicenow["templates"]["setting"] == "Setting change of orders-service"
    texts = servicenow["template_texts"]["setting"]
    assert set(texts) >= {"short_description", "description", "implementation_plan", "backout_plan", "test_plan",
                          "justification", "risk_impact_analysis"}
    assert "Demo system" not in json.dumps(texts)


def test_services_yaml_gives_what_the_steps_read():
    config = ORDERS["app_settings"]
    assert config["keys"] == ["free_delivery_until"] and config["baseline"] == {"free_delivery_until": "2026-11-27"}
    assert config["bake_minutes"] == 2 and config["check_paths"] == ["/", "/api/orders"]
    # The first version must pass its own schema.
    pattern = json.loads((TF / "settings.schema.json").read_text())["properties"]["free_delivery_until"]["pattern"]
    assert re.search(pattern, config["baseline"]["free_delivery_until"])


def test_the_code_names_the_same_application_and_profile_as_services_yaml():
    handler = (ROOT / "demo-states" / "orders-service" / "1.31.0" / "handler.py").read_text()
    config = ORDERS["app_settings"]
    assert f'SETTINGS_APPLICATION = "{config["application"]}"' in handler
    assert f'SETTINGS_PROFILE = "{config["profile"]}"' in handler


def test_terraform_is_its_own_application_and_touches_nothing_of_demo_21():
    text = "\n".join(path.read_text() for path in TF.glob("*.tf"))
    assert "local.settings.application" in text and 'type           = "AWS.Freeform"' in text
    assert 'key          = "release-demo/demos/29-one-setting.tfstate"' in text
    assert 'Demo = "29-one-setting"' in text
    assert "aws_appconfig_extension_association" not in text, "no events: the job records the change itself"
    assert 'name = "release-demo-appconfig-alarm"' in text  # demo 21's role, read only


def test_the_host_may_do_only_what_the_steps_call():
    text = (TF / "iam.tf").read_text()
    granted = set(re.findall(r'"appconfig:(\w+)"', text))
    assert granted == {"StartConfigurationSession", "GetLatestConfiguration", "ListHostedConfigurationVersions",
                       "GetHostedConfigurationVersion", "CreateHostedConfigurationVersion", "ValidateConfiguration",
                       "StartDeployment", "ListDeployments", "GetDeployment", "StopDeployment"}
    assert "*" not in re.sub(r'hostedconfigurationversion/\*|deployment/\*', "", text.split('data "aws_iam_policy_document" "tools_host"')[1])


def test_the_parameter_is_listed_with_no_value():
    parameters = yaml.safe_load((ROOT / "infra" / "parameters.yaml").read_text())
    entry = parameters["appconfig/orders-service-settings"]
    assert entry["source"] == "terraform" and entry["secure"] is False


@pytest.mark.parametrize("path", [
    "scripts/demos/settings/setting.py", "scripts/demos/settings/reset.py", "scripts/demos/settings/demo.py",
    "jenkins/shared-library/vars/settingPipeline.groovy", "jenkins/setting.Jenkinsfile", "infra/demos/29-one-setting/appconfig.tf",
    "infra/demos/29-one-setting/iam.tf", "infra/demos/29-one-setting/main.tf", "demo-states/orders-service/1.31.0/handler.py",
])
def test_no_em_or_en_dash_and_no_demo_label(path):
    text = (ROOT / path).read_text()
    assert chr(0x2014) not in text and chr(0x2013) not in text
    assert "Demo system" not in text
