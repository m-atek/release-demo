import json

import fix
import gitops
import pr_scope
import run_state

MAIN = "image:\n  repository: artifactory-demo.es.nl/docker-local/catalog\n  digest: \"sha256:aa\"\nreplicas: 3\n"
FIVE = MAIN.replace("replicas: 3", "replicas: 5")


def state(**extra):
    found = {
        "service": "catalog", "version": "PR #7 abc1234",
        "pull_request": {"number": 7, "title": "INC0010042 catalog: 5 replicas in production",
                         "url": "https://github.invalid/pull/7", "head_sha": "abc1234" * 5, "branch": "oncall/x",
                         "created_at": "2026-10-30T10:20:00Z"},
        "incident": {"number": "INC0010042", "sys_id": "inc-id"}, "requested_by": "pieter",
        "requester_name": "Pieter On Call", "requester_user": "pieter.oncall",
        "changes": gitops.values_diff(MAIN, FIVE), "build": {"number": "3", "url": "https://jenkins.invalid/3/"},
        "slack": {"channel": "", "ts": ""}, "started_at": 0, "stages": {s: {"status": "waiting"} for s in fix.STAGES},
        "checks": {}, "lines": [],
    }
    found.update(extra)
    return found


def test_the_values_diff_names_the_deployments_field():
    assert gitops.values_diff(MAIN, FIVE) == [{"key": "replicas", "path": "spec.replicas", "from": 3, "to": 5}]
    assert gitops.diff_text(gitops.values_diff(MAIN, FIVE)) == "spec.replicas 5 (was 3)"
    assert gitops.short_diff(gitops.values_diff(MAIN, FIVE)) == "spec.replicas 5"
    assert gitops.values_diff(MAIN, MAIN) == []


def test_the_slack_head_names_the_incident_and_the_editor_not_the_github_author():
    text = fix.render(state())
    head = text.splitlines()[0]
    assert head == ("*Change to catalog production* · <https://github.invalid/pull/7|pull request #7> · INC0010042 · "
                    "requested by Pieter On Call")
    rows = text.split("\n\n")[1].splitlines()
    assert [row.split()[-1] for row in rows] == fix.STAGES


def test_a_failed_check_is_named_under_checks():
    found = state(checks={"Image in JFrog": {"conclusion": "failure", "title": "sha256:bb... isn't in docker-local/catalog",
                                             "failures": []}})
    found["stages"]["Checks"]["status"] = "failed"
    assert "*Image in JFrog*: sha256:bb... isn't in docker-local/catalog" in fix.render(found)


def test_the_emergency_change_says_what_changes_and_how_to_back_out():
    fields = fix.change_fields(state(), {"ci": "ci", "group": "group", "requester": "pieter-id"})
    assert fields["short_description"] == "INC0010042 catalog: 5 replicas in production (#7)"
    assert fields["backout_plan"] == "Revert this pull request; Argo CD syncs the previous values from Git."
    assert "replicas: 3 to 5 (spec.replicas)" in fields["description"] and "INC0010042" in fields["description"]
    assert fields["requested_by"] == "pieter-id" and fields["cmdb_ci"] == "ci"


def test_the_requester_is_the_editor_the_incident_recorded():
    incident = {"description": "Changed: spec.replicas 5, Git 3.\nEdited by pieter-oncall (group "
                               "release-demo:break-glass) with kubectl scale.\n"}
    key, name, user = fix.requester(incident, {"user": {"login": "m-atek"}}, {"author": {"login": "m-atek"}})
    assert (key, name, user) == ("pieter", "Pieter On Call", "pieter.oncall")


def test_without_an_incident_the_author_maps_through_people_yaml():
    key, name, user = fix.requester(None, {"user": {"login": "m-atek"}}, {"author": {"login": "m-atek"}})
    assert name in ("Mateusz", "Sam Developer")


def test_the_incident_number_comes_from_the_title():
    assert gitops.incident_in("INC0010042 catalog: 5 replicas in production") == "INC0010042"
    assert gitops.incident_in("catalog: 5 replicas") is None


def test_only_pull_requests_to_the_folder_are_this_pipelines():
    folder = gitops.settings()["path"]
    assert pr_scope.touches(["deploy/production/catalog/values.yaml"], folder)
    assert not pr_scope.touches(["infra/demos/2-terraform/network/exports.tf"], folder)
    assert not pr_scope.touches(["deploy/production/catalog-old/values.yaml"], folder)


def test_files_outside_the_folder_fail_their_check(monkeypatch, tmp_path):
    run_state.save(state())

    class FakeGitHub:
        def call(self, method, path):
            return [{"filename": "deploy/production/catalog/values.yaml"}, {"filename": "scripts/other.py"}]

    monkeypatch.setattr(fix.github, "GitHub", FakeGitHub)
    fix.files(None)
    result = json.loads((fix.checks_dir() / "only-deploy-production-catalog.json").read_text())
    assert result["conclusion"] == "failure" and result["failures"] == ["scripts/other.py"]


def test_an_image_jfrog_doesnt_have_fails_by_name(monkeypatch):
    import oci

    monkeypatch.setattr(oci.Registry, "tagged", lambda self, repository, reference: None)
    ok, title = gitops.image_in_jfrog({"image": {"repository": "artifactory-demo.es.nl/docker-local/catalog",
                                                 "digest": "sha256:" + "b" * 64}}, "token")
    assert not ok and title == "sha256:bbbbbbbbbbbb... isn't in docker-local/catalog"
    ok, title = gitops.image_in_jfrog({"image": {"repository": "x/docker-local/catalog", "digest": ""}}, "token")
    assert not ok and "isn't a sha256 digest" in title
