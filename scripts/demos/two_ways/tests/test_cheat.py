"""The two cheats and the read: what each writes, and what each refuses to pretend."""

import hashlib
import io
import re
import sys
import zipfile

import pytest

import cheat
import host
from conftest import ADMIN_PASSWORD, ALEX, PACKAGE_PATH, PIPELINE_TOKEN, ROOT, SAM, Now, Store, a_package


def row_of(sn, approver):
    return next(row for row in sn.rows if row["approver"] == approver)


# approve

def test_approve_adds_one_row_in_the_requester_s_name_and_leaves_alex_s(capsys):
    sn = Now()
    cheat.approve(sn, "CHG0030120")
    assert len(sn.rows) == 2
    row = row_of(sn, SAM)
    assert (row["sysapproval"], row["state"], row["source_table"], row["document_id"]) == ("change-id", "approved", "change_request", "change-id")
    assert row_of(sn, ALEX)["state"] == "requested"
    out = capsys.readouterr().out
    assert "CHG0030120: asked by Sam Developer (sam.developer)." in out
    assert "Approval in Sam Developer's name: approved, row written by release-demo.setup." in out


def test_approve_takes_the_requester_from_the_change_not_from_a_name():
    sn = Now(requested_by=ALEX)
    cheat.approve(sn, "CHG0030120")
    assert row_of(sn, ALEX)["state"] == "approved"
    assert not [row for row in sn.rows if row["approver"] == SAM]


def test_approve_twice_finds_the_row_again():
    sn = Now()
    cheat.approve(sn, "CHG0030120")
    cheat.approve(sn, "CHG0030120")
    assert len(sn.rows) == 2


def test_a_rule_that_keeps_a_new_approval_at_requested_gets_the_state_set_after_it():
    sn = Now(forced_state="requested")
    cheat.approve(sn, "CHG0030120")
    assert row_of(sn, SAM)["state"] == "approved"


def test_if_servicenow_never_shows_it_approved_it_says_so(capsys):
    sn = Now(forced_state="never approved")
    with pytest.raises(SystemExit):
        cheat.approve(sn, "CHG0030120")
    assert "doesn't show the approval" in capsys.readouterr().err


@pytest.mark.parametrize("number, change, message", [
    ("CHG0000000", {}, "no change CHG0000000"),
    ("CHG0030120", {"state": "4", "approval": "not requested"}, "isn't waiting for approval"),
    ("CHG0030120", {"requested_by": ""}, "names nobody"),
])
def test_approve_refuses_what_isn_t_a_waiting_change_with_a_requester(number, change, message, capsys):
    sn = Now()
    sn.change.update(change)
    with pytest.raises(SystemExit):
        cheat.approve(sn, number)
    assert message in capsys.readouterr().err
    assert len(sn.rows) == 1


def test_approve_signs_in_as_the_setup_user_from_where_you_are(monkeypatch):
    read = []
    monkeypatch.setattr(cheat, "ssm_client", lambda: None)
    monkeypatch.setattr(cheat, "get_parameter", lambda ssm, name: read.append(name) or "https://servicenow.example")
    sn = cheat.admin_servicenow()
    assert read == ["servicenow/instance-url", "servicenow/admin-password"]
    assert sn.url == "https://servicenow.example"


def test_the_tools_host_may_not_read_the_admin_password_so_approve_never_hands_over(monkeypatch):
    """infra/tools_host.tf's NotForTheHost names it: approve runs on the Mac, with your sign-in."""
    policy = (ROOT / "infra" / "tools_host.tf").read_text()
    assert re.search(r'sid\s+= "NotForTheHost"[\s\S]{0,200}"servicenow/admin-password"', policy)
    sn = Now()
    monkeypatch.setattr(cheat, "admin_servicenow", lambda: sn)
    monkeypatch.setattr(cheat.host, "hand_over", lambda *args, **kwargs: pytest.fail("approve must not hand over"))
    monkeypatch.setattr(sys, "argv", ["cheat.py", "approve", "CHG0030120"])
    assert cheat.main() == 0


# swap

@pytest.fixture
def jfrog(monkeypatch):
    store = Store({PACKAGE_PATH: a_package()})
    monkeypatch.setattr(cheat.publish, "request", store.as_pipeline)
    monkeypatch.setattr(cheat, "get_parameter", lambda ssm, name: {"jcr/admin-password": ADMIN_PASSWORD, "jcr/token": PIPELINE_TOKEN}[name])
    return store


def swap(store, build="123", version="1.30.0"):
    return cheat.swap(build, version, ssm=object(), request=store.as_admin)


def test_swap_tries_the_pipeline_s_token_first_then_overwrites_as_admin(jfrog, capsys):
    before = jfrog.files[PACKAGE_PATH]
    assert swap(jfrog) == 0
    assert jfrog.puts == [("release-pipeline", PACKAGE_PATH), ("admin", PACKAGE_PATH)]
    after = jfrog.files[PACKAGE_PATH]
    assert hashlib.sha256(after).hexdigest() != hashlib.sha256(before).hexdigest()
    assert jfrog.deployed_by[PACKAGE_PATH] == "admin"
    out = capsys.readouterr().out
    assert f"Stored now: SHA-256 {hashlib.sha256(before).hexdigest()}, deployed by release-pipeline." in out
    assert "The pipeline's own token: refused, 403." in out
    assert f"Stored now: SHA-256 {hashlib.sha256(after).hexdigest()}, deployed by admin." in out


def test_the_swapped_package_is_the_same_package_plus_one_harmless_file(jfrog):
    before = zipfile.ZipFile(io.BytesIO(jfrog.files[PACKAGE_PATH]))
    swap(jfrog)
    after = zipfile.ZipFile(io.BytesIO(jfrog.files[PACKAGE_PATH]))
    assert set(after.namelist()) - set(before.namelist()) == {cheat.EXTRA_FILE}
    assert all(after.read(name) == before.read(name) for name in before.namelist())
    assert after.read(cheat.EXTRA_FILE).decode() == cheat.EXTRA_TEXT


def test_swap_twice_writes_nothing_more(jfrog, capsys):
    swap(jfrog)
    swapped, written = jfrog.files[PACKAGE_PATH], len(jfrog.puts)
    capsys.readouterr()
    assert swap(jfrog) == 0
    assert jfrog.files[PACKAGE_PATH] == swapped and len(jfrog.puts) == written
    assert "Already swapped" in capsys.readouterr().out


def test_if_the_pipeline_s_token_can_overwrite_the_story_is_wrong_and_it_stops(jfrog, capsys):
    jfrog.pipeline_may_overwrite = True
    assert swap(jfrog) == 3
    assert jfrog.puts == [("release-pipeline", PACKAGE_PATH)]
    assert "script's line about its token is wrong" in capsys.readouterr().out


def test_another_answer_from_the_pipeline_s_token_isn_t_called_a_refusal(jfrog, monkeypatch, capsys):
    monkeypatch.setattr(cheat.publish, "request", lambda *args, **kwargs: (401, b"bad token"))
    assert swap(jfrog) == 0
    out = capsys.readouterr().out
    assert "answered 401, not 403" in out and "refused" not in out.replace('"refused"', "")


def test_a_refused_admin_write_and_a_missing_package_both_say_so(jfrog, capsys):
    jfrog.admin_refused = True
    with pytest.raises(SystemExit):
        swap(jfrog)
    assert "refused the admin's overwrite (500)" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        swap(jfrog, build="999")
    assert "has no orders-service-candidates/1.30.0/999" in capsys.readouterr().err


def test_the_path_is_the_one_publish_py_stores_for_that_build():
    assert cheat.package_path("123") == PACKAGE_PATH


def test_from_the_mac_swap_hands_over_and_on_the_host_it_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(cheat.host, "on_host", lambda: False)
    monkeypatch.setattr(cheat.host, "hand_over", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr(sys, "argv", ["cheat.py", "swap", "123"])
    assert cheat.main() == 0
    assert calls == [(("two-ways-swap", "demos/two_ways/cheat.py", ["swap", "123", "--version", "1.30.0"]), {"title": "cheat.py swap 123"})]
    ran = []
    monkeypatch.setattr(cheat, "swap", lambda build, version: ran.append((build, version)) or 0)
    monkeypatch.setattr(sys, "argv", ["cheat.py", "--here", "swap", "123", "--version", "1.30.0"])
    assert cheat.main() == 0
    assert ran == [("123", "1.30.0")] and len(calls) == 1


def test_hand_over_s_own_command_line_parses(monkeypatch):
    """host.hand_over runs scripts/<script> --here <args>, with this name a unit name and a log file."""
    assert re.fullmatch(r"[a-z-]+", "two-ways-swap")
    assert host.REPO_DIR and cheat.SCRIPT == "demos/two_ways/cheat.py"
    assert (ROOT / "scripts" / cheat.SCRIPT).exists()


def test_no_secret_reaches_the_output(jfrog, capsys):
    swap(jfrog)
    cheat.approve(Now(), "CHG0030120")
    out = capsys.readouterr()
    assert PIPELINE_TOKEN not in out.out + out.err and ADMIN_PASSWORD not in out.out + out.err


# production

class Paginator:
    def __init__(self, versions):
        self.versions = versions

    def paginate(self, FunctionName):
        return [{"Versions": self.versions[:2]}, {"Versions": self.versions[2:]}]


class ProductionReads:
    def __init__(self, versions, live):
        self.versions, self.live, self.calls = versions, live, []

    def __getattr__(self, name):
        if name in ("get_alias", "get_paginator"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"production() is read only, and {name} isn't one of its two reads")

    def get_alias(self, FunctionName, Name):
        self.calls.append(FunctionName)
        return {"FunctionVersion": self.live}

    def get_paginator(self, name):
        assert name == "list_versions_by_function"
        return Paginator(self.versions)


def test_production_reads_what_is_live_and_whether_the_take_s_version_got_there(capsys):
    versions = [{"Version": "$LATEST"}, {"Version": "15", "Description": "1.3.0"}, {"Version": "37", "Description": "1.26.0"}]
    seen = cheat.production(ProductionReads(versions, "37"))
    assert seen == {"live": "37", "running": "1.26.0", "newest": 37, "as_take_version": 0}
    assert capsys.readouterr().out == "orders-service-production: live is version 37 (1.26.0), newest published 37, 0 published as 1.30.0.\n"
