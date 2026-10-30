"""Demo 14: .github/workflows/release.yml against Jenkins' release pipeline.
The same scripts in the same order, the same stage wrapper, the same pinned
images; started only by a merge to main; least privilege for every job; no
secrets. Nothing runs: the files are read.

    .venv/bin/python -m pytest -q scripts/tests/test_github_actions.py
"""

import re
from pathlib import Path

import pytest
import yaml

import pipeline_calls

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "release.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_FILE.read_text())
JOBS = WORKFLOW["jobs"]
STAGE_SH = (ROOT / ".github" / "release-stage.sh").read_text()
TOOLCHAIN = (ROOT / "jenkins" / "shared-library" / "vars" / "toolchain.groovy").read_text()


def scripts(job_names):
    return "\n".join(step.get("run", "") for name in job_names for step in JOBS[name]["steps"])


def shell_function(text, name):
    found = re.search(rf"^{name}\(\) \{{[^\n]*\n(.*?)^\}}", text, re.M | re.S)
    assert found, f"no function {name}"
    return found.group(1)


def test_the_jobs_call_the_same_scripts_in_the_same_order_as_jenkins():
    release_jobs = [name for name in JOBS if name != "stopped"]
    assert pipeline_calls.calls(scripts(release_jobs)) == pipeline_calls.jenkins()


def test_stopped_does_what_jenkins_stopped_does():
    assert set(pipeline_calls.calls(scripts(["stopped"]))) == set(pipeline_calls.stopped_calls())
    stopped = JOBS["stopped"]
    assert stopped["if"] == "failure() || cancelled()"
    assert set(stopped["needs"]) == set(JOBS) - {"stopped"}


def test_every_stage_is_wrapped_as_release_stage_wraps_it_in_jenkins_order():
    text = scripts(JOBS)
    named = re.findall(r"^\s*(?:STAGE_STARTED=\S+\s+)?(?:stage|skip) (\"[^\"]+\"|[A-Z]\w*)", text, re.M)
    order = list(dict.fromkeys(name.strip('"') for name in named))
    assert order == pipeline_calls.jenkins_stages()


def test_the_stage_wrapper_makes_release_stages_calls():
    around, skipped = pipeline_calls.stage_calls()
    assert pipeline_calls.calls(shell_function(STAGE_SH, "stage"), wrapper=True) == around
    assert pipeline_calls.calls(shell_function(STAGE_SH, "skip"), wrapper=True) == skipped


def test_only_a_merge_to_main_that_changes_the_service_starts_it():
    on = WORKFLOW[True]  # YAML 1.1 reads the key "on" as true
    assert on == {"push": {"branches": ["main"], "paths": ["services/orders-service/**"]}}
    assert JOBS["build"]["if"] == "startsWith(github.event.head_commit.message, 'ORD-')"


def test_one_release_at_a_time():
    assert WORKFLOW["concurrency"] == {"group": "release-orders-service", "cancel-in-progress": False}


def test_no_job_gets_more_than_it_needs():
    assert WORKFLOW["permissions"] == {}
    for name, job in JOBS.items():
        expected = {"contents": "read", "id-token": "write"}
        if name == "build":
            expected["pull-requests"] = "read"  # the pull request's label standard
        if name == "publish":
            expected["attestations"] = "write"
        assert job["permissions"] == expected, name


def test_each_job_signs_in_as_the_role_for_its_environment():
    for name, job in JOBS.items():
        roles = [step["with"]["role-to-assume"] for step in job["steps"]
                 if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")]
        role = {"staging": "staging", "production": "production"}.get(job.get("environment"), "ci")
        assert roles == [f"arn:aws:iam::${{{{ vars.AWS_ACCOUNT_ID }}}}:role/release-demo-github-{role}"], name
        assert job.get("environment") in (None, "staging", "production")
    assert JOBS["staging"]["environment"] == "staging" and JOBS["production"]["environment"] == "production"


def test_github_stores_no_secrets_for_it():
    assert "secrets." not in WORKFLOW_FILE.read_text()


def test_the_images_are_toolchain_groovys_by_the_same_digests():
    pinned = dict(re.findall(r"@Field String (SONAR|SYFT|GRYPE) = '([^']+)'", TOOLCHAIN))
    used = {}
    for job in JOBS.values():
        for step in job["steps"]:
            used.update({k: v for k, v in step.get("env", {}).items() if k in ("SONAR", "SYFT", "GRYPE")})
    assert used == pinned
    assert all("@sha256:" in image for image in used.values())


def test_every_action_names_an_exact_release_for_the_lockfile_to_pin():
    uses = [step["uses"] for job in JOBS.values() for step in job["steps"] if "uses" in step]
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+\.\d+\.\d+", u) for u in uses), uses


def test_the_lockfile_check_comes_first_in_build():
    steps = JOBS["build"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert "gh actions-lock --verify" in steps[1]["run"]


def test_the_lockfile_covers_every_action(request):
    lockfile = ROOT / ".github" / "workflows" / "actions.lock"
    if not lockfile.exists():
        pytest.skip("actions.lock comes from gh actions-lock, run once after the merge (prep file, video 14)")
    locked = yaml.safe_load(lockfile.read_text())["workflows"][".github/workflows/release.yml"]
    uses = {step["uses"] for job in JOBS.values() for step in job["steps"] if "uses" in step}
    assert uses <= set(locked)


def test_the_package_is_attested_before_it_is_published():
    steps = [step.get("uses", step.get("name")) for step in JOBS["publish"]["steps"]]
    assert steps.index(next(s for s in steps if s.startswith("actions/attest@"))) < steps.index("Publish")


def test_the_run_state_passes_encrypted_and_only_after_the_job_had_it():
    for name, job in JOBS.items():
        names = [step.get("name", step.get("uses", "")) for step in job["steps"]]
        if name == "stopped":
            continue
        upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        assert upload["with"] == {"name": "run-state", "path": "run-state.enc", "overwrite": True, "retention-days": 1}
        assert "Run state for the next job" in names
    assert "openssl enc -aes-256-cbc" in shell_function(STAGE_SH, "save_state")
