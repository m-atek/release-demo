"""Demo 22: azure-pipelines.yml against Jenkins' release pipeline. The same
scripts in the same order, the same stage wrapper, the same pinned images;
queued only by the Slack handler, with the Jenkins job's parameters; on the
self-hosted pool only; production's steps named as the video shows them; no
secrets. Nothing runs: the files are read.

    .venv/bin/python -m pytest -q scripts/tests/test_azure_pipelines.py
"""

import re
from pathlib import Path

import yaml

import pipeline_calls

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "azure-pipelines.yml").read_text()
PIPELINE = yaml.safe_load(TEXT)
STAGE_TEMPLATE = yaml.safe_load((ROOT / "azure-pipelines" / "stage.yml").read_text())
STAGES = {stage["stage"]: stage for stage in PIPELINE["stages"]}
TOOLCHAIN = (ROOT / "jenkins" / "shared-library" / "vars" / "toolchain.groovy").read_text()


def steps(stage):
    for job in stage["jobs"]:
        yield from job.get("steps") or job["strategy"]["runOnce"]["deploy"]["steps"]


def script(step):
    """A step's shell: a bash step's own, or the script a stage template step runs."""
    if "bash" in step:
        return step["bash"]
    if step.get("template") == "azure-pipelines/stage.yml":
        return step["parameters"]["script"]
    return ""


def text(stage_names):
    return "\n".join(script(step) for name in stage_names for step in steps(STAGES[name]))


def test_the_stages_call_the_same_scripts_in_the_same_order_as_jenkins():
    release = [name for name in STAGES if name != "Stopped"]
    assert pipeline_calls.calls(text(release)) == pipeline_calls.jenkins()


def test_stopped_does_what_jenkins_stopped_does_and_says_when_a_run_was_cancelled():
    stopped = STAGES["Stopped"]
    assert stopped["condition"] == "or(failed(), canceled())"
    assert set(stopped["dependsOn"]) == set(STAGES) - {"Stopped"}
    calls = pipeline_calls.calls(text(["Stopped"]))
    assert set(calls) == set(pipeline_calls.stopped_calls())
    assert "notify.py finish cancelled" in TEXT


def test_the_step_template_makes_release_stages_calls():
    around, skipped = pipeline_calls.stage_calls()
    assert pipeline_calls.calls(STAGE_TEMPLATE["steps"][0]["bash"], wrapper=True) == around
    change = text(["Change"])
    assert pipeline_calls.calls(change[change.index("standard"):change.index("else")], wrapper=True) == skipped


def test_the_slack_stages_in_jenkins_order():
    named = []
    for stage in PIPELINE["stages"]:
        for step in steps(stage):
            if step.get("template") == "azure-pipelines/stage.yml":
                named.append(step["parameters"]["stage"])
    assert list(dict.fromkeys(named)) == pipeline_calls.jenkins_stages()
    assert [stage["displayName"] for stage in PIPELINE["stages"]] == [
        "Build", "Test", "Quality gate", "SBOM and scan", "Publish", "Staging", "Change", "Production", "Stopped"]


def test_productions_steps_are_the_ones_the_video_shows():
    templated = [step for step in steps(STAGES["Production"]) if step.get("template") == "azure-pipelines/stage.yml"]
    assert [step["parameters"]["displayName"] for step in templated] == [
        "Wait for the ServiceNow approval", "Mark the change in progress", "Deploy to production", "Check production",
        "Record"]
    wait = templated[0]
    assert wait["parameters"]["script"].splitlines()[1] == "python3 scripts/change.py wait || return 1"


def test_only_the_slack_handler_queues_it():
    assert PIPELINE["trigger"] == "none" and PIPELINE["pr"] == "none"


def test_its_parameters_are_the_jenkins_jobs():
    jenkins = pipeline_calls.release_parameters()
    camel = [re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name.lower()) for name in jenkins]
    assert [p["name"] for p in PIPELINE["parameters"]] == camel
    for name, parameter in zip(jenkins, camel):
        assert PIPELINE["variables"][name] == f"${{{{ parameters.{parameter} }}}}"


def test_the_first_stage_refuses_any_other_service_in_one_line():
    first = next(step for step in steps(STAGES["Build"]) if "bash" in step)["bash"].splitlines()[0]
    assert first == '[ "$SERVICE" = orders-service ] || { echo "This pipeline releases orders-service, not $SERVICE."; exit 1; }'


def test_every_job_runs_on_the_self_hosted_pool():
    assert PIPELINE["pool"] == "release-demo"
    assert "vmImage" not in TEXT and not [stage for stage in PIPELINE["stages"] for job in stage["jobs"] if "pool" in job]


def test_staging_and_production_are_deployment_jobs_on_their_environments():
    assert [job.get("environment") for job in STAGES["Staging"]["jobs"]] == ["staging"]
    assert [job.get("environment") for job in STAGES["Production"]["jobs"]] == ["production"]
    assert not [job for name, stage in STAGES.items() if name not in ("Staging", "Production")
                for job in stage["jobs"] if "environment" in job]


def test_the_ci_adapter_gets_azures_names_for_the_run():
    assert {k: v for k, v in PIPELINE["variables"].items() if k.startswith("RELEASE_")} == {
        "RELEASE_CI_NAME": "Azure Pipelines run", "RELEASE_RUN_ID": "$(Build.BuildId)",
        "RELEASE_RUN_URL": "$(System.CollectionUri)$(System.TeamProject)/_build/results?buildId=$(Build.BuildId)",
        "RELEASE_BUILD_ID": "ado-$(Build.BuildId)", "RELEASE_COMMIT": "$(Build.SourceVersion)"}
    assert ('RELEASE_CORRELATION_ID="azure-pipelines:$(basename "$SYSTEM_COLLECTIONURI")/$SYSTEM_TEAMPROJECT:$BUILD_BUILDID"'
            in TEXT)


def test_the_change_names_the_run():
    assert "##vso[build.updatebuildnumber]$SERVICE $(python3 scripts/run_state.py get version) · " in TEXT


def test_the_images_are_toolchain_groovys_by_the_same_digests():
    pinned = dict(re.findall(r"@Field String (SONAR|SYFT|GRYPE) = '([^']+)'", TOOLCHAIN))
    assert {k: PIPELINE["variables"][k] for k in pinned} == pinned


def test_azure_devops_holds_no_secrets():
    assert not re.search(r"variables:\s*\n\s*- group:|DownloadSecureFile|isSecret|\$\(System\.AccessToken\)", TEXT)
    assert "SONAR_TOKEN=$(release-demo-parameter sonar/token)" in TEXT
