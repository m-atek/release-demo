import json

import stack
from conftest import plan_with


def test_the_summary_counts_a_replace_as_an_add_and_a_destroy():
    summary = stack.plan_summary(plan_with(
        ("aws_s3_bucket.exports", ["create"]), ("aws_sqs_queue.exports", ["update"]),
        ("aws_subnet.private[\"a\"]", ["delete", "create"]), ("aws_vpc.main", ["no-op"]),
        ("aws_security_group.old", ["delete"])))
    assert stack.summary_line(summary) == "2 to add, 1 to change, 2 to destroy"
    assert [r["action"] for r in summary["resources"]] == ["create", "update", "replace", "destroy"]


def test_no_changes():
    summary = stack.plan_summary(plan_with(("aws_vpc.main", ["no-op"]), ("data.x", ["read"])))
    assert not stack.has_changes(summary)
    assert stack.summary_line(summary) == "0 to add, 0 to change, 0 to destroy"


def test_money():
    assert stack.money("6.6", sign=True) == "+$6.60"
    assert stack.money("-1.204", sign=True) == "-$1.20"
    assert stack.money("0", sign=True) == "$0.00"
    assert stack.money("1234.5") == "$1,234.50"


def test_scrub_hides_account_ids():
    assert stack.scrub("arn:aws:iam::123456789012:role/x is denied") == "arn:aws:iam::<account>:role/x is denied"


def test_credentials_are_written_for_this_user_only(monkeypatch, work):
    expires = __import__("datetime").datetime(2026, 10, 27, 18, 0, tzinfo=__import__("datetime").timezone.utc)
    monkeypatch.setattr(stack, "assume", lambda kind: {"AccessKeyId": "AKIAEXAMPLE", "SecretAccessKey": "example",
                                                       "SessionToken": "token", "Expiration": expires})
    monkeypatch.setattr(stack, "settings", lambda: {"plan_role": "plan-role", "apply_role": "apply-role"})
    stack.write_credentials("plan")
    stack.write_credentials("apply")
    text = stack.credentials_file().read_text()
    assert "[network-plan]" in text and "[network-apply]" in text
    assert stack.credentials_file().stat().st_mode & 0o777 == 0o600


def test_the_services_entry_names_the_roles_and_the_demo_pull_request():
    settings = stack.settings()
    assert settings["plan_role"].startswith("release-demo-deploy-") and settings["apply_role"].startswith("release-demo-deploy-")
    assert settings["demo_branch"].startswith("demo-2/")
    assert settings["demo_title"] == "ORD-22 Store exports bucket and queue"


def test_the_demo_states_differ_only_in_public_access():
    states = stack.ROOT / "demo-states" / "network-stack"
    public = (states / "exports-public" / "exports.tf").read_text()
    private = (states / "exports-private" / "exports.tf").read_text()
    assert "Principal = \"*\"" in public and "= false" in public
    assert "Principal = \"*\"" not in private and "= false" not in private
    assert json.dumps(public).count("aws_sqs_queue\\\" \\\"exports") == 1
