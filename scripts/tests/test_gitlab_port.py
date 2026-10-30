"""Demo 17: .gitlab-ci.yml, the port, against Jenkins' release pipeline. The
same scripts in the same order, the same stage wrapper, the same pinned
images; started only by a trigger, with the Jenkins job's parameters as its
inputs; on the runner tools-host only; no secrets. Nothing runs: the files
are read.

    .venv/bin/python -m pytest -q scripts/tests/test_gitlab_port.py
"""

import importlib.util
import re
from pathlib import Path

import yaml

import pipeline_calls

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / ".gitlab-ci.yml").read_text()
HEADER_TEXT, BODY_TEXT = TEXT.split("\n---\n", 1)


class Loader(yaml.SafeLoader):
    """GitLab's !reference tag, as the names it points at."""


Loader.add_constructor("!reference", lambda loader, node: {"!reference": loader.construct_sequence(node)})

HEADER = yaml.safe_load(HEADER_TEXT)
PIPELINE = yaml.load(BODY_TEXT, Loader=Loader)
JOBS = {name: job for name, job in PIPELINE.items() if isinstance(job, dict) and "stage" in job}
TOOLCHAIN = (ROOT / "jenkins" / "shared-library" / "vars" / "toolchain.groovy").read_text()


def lines(job, keys=("before_script", "script", "after_script")):
    return [line for key in keys for line in job.get(key, []) if isinstance(line, str)]


def text(names):
    return "\n".join(line for name in names for line in lines(JOBS[name]))


def test_the_jobs_call_the_same_scripts_in_the_same_order_as_jenkins():
    release_jobs = [name for name in JOBS if name != "Stopped"]
    assert pipeline_calls.calls(text(release_jobs)) == pipeline_calls.jenkins()


def test_stopped_runs_on_failure_and_does_what_jenkins_stopped_does():
    assert JOBS["Stopped"]["when"] == "on_failure"
    assert set(pipeline_calls.calls(text(["Stopped"]))) == set(pipeline_calls.stopped_calls())


def test_the_stage_template_makes_release_stages_calls():
    around, skipped = pipeline_calls.stage_calls()
    template = PIPELINE[".stage"]
    assert pipeline_calls.calls("\n".join(lines(template)), wrapper=True) == around
    change = "\n".join(lines(JOBS["Change"]))
    assert pipeline_calls.calls(change[change.index("standard"):], wrapper=True) == skipped
    for name, job in JOBS.items():
        if name != "Stopped":
            assert ".stage" in ([job["extends"]] if isinstance(job["extends"], str) else job["extends"]), name
            assert job["variables"]["STAGE"] == name


def test_one_job_per_stage_in_slacks_order():
    assert PIPELINE["stages"] == [*pipeline_calls.jenkins_stages(), "Stopped"]
    assert [job["stage"] for job in JOBS.values()] == PIPELINE["stages"]
    assert list(JOBS) == PIPELINE["stages"]


def test_only_a_trigger_starts_it():
    assert PIPELINE["workflow"] == {"rules": [{"if": '$CI_PIPELINE_SOURCE == "trigger"'}]}


def test_its_inputs_are_the_jenkins_jobs_parameters():
    jobs_groovy = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    release = jobs_groovy[jobs_groovy.index("pipelineJob('release')"):jobs_groovy.index("pipelineJob('rollback')")]
    parameters = re.findall(r"stringParam\('(\w+)'", release)
    assert list(HEADER["spec"]["inputs"]) == [p.lower() for p in parameters]
    for parameter in parameters:
        assert PIPELINE["variables"][parameter] == f"$[[ inputs.{parameter.lower()} ]]"


def test_the_slack_handler_sends_exactly_these_inputs():
    spec = importlib.util.spec_from_file_location("slack_handler_for_gitlab", ROOT / "slack" / "handler.py")
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    assert sorted(handler.GITLAB_INPUTS.values()) == sorted(HEADER["spec"]["inputs"])


def test_the_first_job_refuses_any_other_service_in_one_line():
    first = JOBS["Build"]["before_script"][0]
    assert first == '[ "$SERVICE" = orders-service ] || { echo "This pipeline releases orders-service, not $SERVICE."; exit 1; }'


def test_the_ci_adapter_gets_gitlabs_names_for_the_run():
    assert {k: v for k, v in PIPELINE["variables"].items() if k.startswith("RELEASE_")} == {
        "RELEASE_CI_NAME": "GitLab pipeline", "RELEASE_RUN_ID": "$CI_PIPELINE_ID", "RELEASE_RUN_URL": "$CI_PIPELINE_URL",
        "RELEASE_BUILD_ID": "gl-$CI_PIPELINE_IID", "RELEASE_COMMIT": "$CI_COMMIT_SHA",
        "RELEASE_CORRELATION_ID": "gitlab-ci:$CI_PROJECT_PATH:$CI_PIPELINE_ID"}


def test_the_images_are_toolchain_groovys_by_the_same_digests():
    pinned = dict(re.findall(r"@Field String (SONAR|SYFT|GRYPE) = '([^']+)'", TOOLCHAIN))
    assert {k: PIPELINE["variables"][k] for k in pinned} == pinned
    # The toolchain image under toolchain.groovy's tag: release-demo/python-tools:<hash of its inputs>.
    assert "cat '${dockerfile}' requirements-dev.txt | sha256sum | cut -c1-12" in TOOLCHAIN
    assert 'cat "$dockerfile" requirements-dev.txt | sha256sum | cut -c1-12' in TEXT
    assert "local dockerfile=jenkins/toolchain/python.Dockerfile" in TEXT and "image=release-demo/python-tools:$(" in TEXT


def test_every_job_runs_on_tools_host_alone():
    assert PIPELINE["default"]["tags"] == ["tools-host"]
    assert not [name for name, job in JOBS.items() if "tags" in job or "image" in job]


def test_one_release_at_a_time_from_staging_to_record():
    grouped = [name for name, job in JOBS.items() if ".one-release" in (job.get("extends") or [])]
    assert grouped == ["Staging", "Check staging", "Change", "Approval", "Production", "Check production", "Record"]
    assert PIPELINE[".one-release"] == {"resource_group": "orders-service-release"}


def test_the_approval_job_waits_on_servicenow_as_jenkins_does():
    assert JOBS["Approval"]["script"][:2] == [
        'echo "Waiting for ServiceNow change $(python3 scripts/run_state.py get change.number)."',
        "python3 scripts/change.py wait"]
    assert JOBS["Approval"]["rules"] == [{"if": '$OPTION != "standard"'}]


def test_the_run_state_passes_as_artifacts_for_a_day_and_the_tests_show_in_gitlab():
    assert PIPELINE["default"]["artifacts"] == {"paths": [".release/", "dist/"], "when": "always", "expire_in": "1 day"}
    assert JOBS["Test"]["artifacts"]["reports"] == {"junit": ".release/reports/junit.xml"}


def test_gitlab_holds_no_secrets_and_the_scanners_token_comes_from_the_helper():
    assert "SONAR_TOKEN=$(release-demo-parameter sonar/token)" in TEXT
    assert not re.search(r"glpat-|glptt-|glrt-|\$CI_JOB_TOKEN|secrets:", TEXT)
    assert not [k for k in PIPELINE["variables"] if "TOKEN" in k or "PASSWORD" in k or "SECRET" in k]
