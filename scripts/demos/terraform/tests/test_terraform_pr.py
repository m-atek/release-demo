import json
import time

import checks
import github
import pr
import stack


def state(**extra):
    base = {
        "service": "network-stack", "version": "PR #7 abc1234",
        "pull_request": {"number": 7, "title": "ORD-22 Store exports bucket and queue",
                         "url": "https://github.com/o/r/pull/7", "head_sha": "abc1234" + "0" * 33, "branch": "demo-2/x"},
        "commit": "abc1234" + "0" * 33, "commit_time": int(time.time()) - 60, "jira_key": "ORD-22",
        "requested_by": "sam", "requester_name": "Sam Developer", "requester_slack": "U0123456789",
        "build": {"number": "3", "url": "https://jenkins.example/job/3/"}, "slack": {"channel": "", "ts": ""},
        "started_at": time.time() - 200, "stages": {n: {"status": "waiting"} for n in pr.STAGES},
        "checks": {}, "lines": [],
    }
    base.update(extra)
    return base


def failed_checks():
    found = {name: {"conclusion": "success", "title": "ok", "failures": []} for name in checks.CHECKS}
    found["Checkov"] = {"conclusion": "failure", "title": "6 failed",
                        "failures": ["CKV_AWS_53 Ensure S3 bucket has block public ACLS enabled"]}
    found["Conftest"] = {"conclusion": "failure", "title": "Failed: S3 bucket must not be public",
                         "failures": ["S3 bucket must not be public"]}
    return found


def test_slack_names_the_failed_checks_and_the_rule():
    text = pr.render(state(checks=failed_checks()))
    assert "*Infrastructure change: ORD-22 Store exports bucket and queue*" in text
    assert "requested by <@U0123456789>" in text
    assert ":x:  *Checkov*: CKV_AWS_53 Ensure S3 bucket has block public ACLS enabled" in text
    assert ":x:  *Conftest*: S3 bucket must not be public" in text


def test_the_gate_blocks_and_says_so(monkeypatch, work):
    stack.save(state())
    for name, result in failed_checks().items():
        stack.write_json(checks.result_path(name), {"name": name, **result, "summary": "", "text": ""})
    posted, sent = [], []
    monkeypatch.setattr(pr, "ssm_client", lambda: None)
    monkeypatch.setattr(pr, "post", lambda s, ssm=None: posted.append(pr.render(s)))
    monkeypatch.setattr(pr, "send_event", lambda ssm, s, stage, result, *a, **k: sent.append((stage, result)))
    try:
        pr.gate(None)
    except SystemExit as stop:
        assert stop.code == 1
    else:
        raise AssertionError("the gate let failed checks through")
    assert "*Blocked*: Checkov and Conftest failed. The merge stays blocked until they pass." in posted[-1]
    assert sent == [("Checks", "failed")]
    assert stack.load()["result"] == "blocked"


def test_the_comment_has_the_summary_and_the_cost_and_no_internal_links():
    summary = {"add": 2, "change": 0, "destroy": 0, "resources": [
        {"action": "create", "address": "aws_s3_bucket.exports"}, {"action": "create", "address": "aws_sqs_queue.exports"}]}
    passing = {name: {"conclusion": "success", "title": "ok", "failures": []} for name in checks.CHECKS}
    body = pr.comment_body(state(plan=summary, checks=passing, cost={
        "status": "priced", "main": "0", "branch": "6.6", "difference": "6.6"}), "Next: a change.")
    assert "**2 to add, 0 to change, 0 to destroy** · monthly cost **+$6.60** (main $0.00, this pull request $6.60)" in body
    assert "| + create | `aws_s3_bucket.exports` |" in body
    assert "service-now.com" not in body and "atlassian.net" not in body and "jenkins.example" not in body


def test_an_unpriced_cost_says_why():
    assert pr.cost_line({"status": "unpriced", "reason": "no token"}) == "not priced: no token"


def test_the_requester_is_sam_for_mateuszs_account():
    pull = {"user": {"login": "release-demo-m-atek[bot]"}}
    key, person = github.requester(pull, {"author": {"login": "m-atek"}})
    assert key == "sam" and person["display_name"] == "Sam Developer"


def test_a_stale_plan_exits_3(monkeypatch, work):
    stack.save(state(plan={"add": 1, "change": 0, "destroy": 0, "resources": []}))
    (work / "reports").mkdir(parents=True)

    class Result:
        returncode, stdout = 1, ""
        stderr = "Error: Saved plan is stale\n\nThe given plan file can no longer be applied because the state was changed"

    monkeypatch.setattr(stack, "terraform", lambda *a, **k: Result())
    try:
        pr.apply(None)
    except SystemExit as stop:
        assert stop.code == pr.STALE
    else:
        raise AssertionError("a stale plan was applied")


def test_infracost_totals(monkeypatch, work, tmp_path):
    (work / "reports").mkdir(parents=True)

    class Result:
        returncode, stderr = 0, ""
        stdout = json.dumps({"currency": "USD", "summary": {"total_monthly_cost": "6.6"}, "projects": []})

    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: Result())
    assert str(pr.infracost_total(tmp_path, "branch")) == "6.6"
    Result.stdout = json.dumps({"projects": [{"resources": [{"cost_components": [
        {"total_monthly_cost": "4.6"}, {"total_monthly_cost": "2"}]}]}]})
    assert str(pr.infracost_total(tmp_path, "main")) == "6.6"
