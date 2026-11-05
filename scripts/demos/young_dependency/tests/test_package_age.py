"""The stage: what it reads, what it decides, every Slack line, and toward a person when it can't read."""

import json
import subprocess
import sys

import pytest

import build
import package_age
import run_state
from conftest import HOUR, NOW, OLDER, SAM, SEVEN, Index, real_index, resolved, sha, stamp, state_for


@pytest.fixture(autouse=True)
def checkout(tmp_path, monkeypatch):
    """The stage's checkout: orders-service on main is 1.3.0 here, with no requirements.txt."""
    from common import service

    config = {**service("orders-service"), "dir": tmp_path}
    (tmp_path / "requirements.txt").write_text("boto3==1\n")
    monkeypatch.setattr(package_age, "service", lambda name: config)
    return tmp_path


def stage(state, pairs, index=None, now=NOW, tmp=None):
    state = state or state_for()
    return package_age.run_stage(state, "orders-service", now=now, read=index or real_index(),
                                 resolver=lambda requirements, runtime: resolved(*pairs)), state


# The words.

@pytest.mark.parametrize("seconds,text", [
    (60, "1m"), (61, "2m"), (42 * 60, "42m"), (3600, "1h 00m"), (3600 + 8 * 60, "1h 08m"), (9 * HOUR + 41 * 60 - 1, "9h 41m"),
    (71 * HOUR + 59 * 60, "71h 59m"), (72 * HOUR, "3d 00h"), (73 * HOUR, "3d 01h"), (73 * HOUR + 1, "3d 02h"), (978 * 24 * HOUR, "978d 00h"),
])
def test_age_is_never_rounded_down(seconds, text):
    assert package_age.age_text(seconds) == text


# The decision.

def test_exactly_at_the_rule_passes_and_a_second_less_is_refused():
    settings = {"min_hours": 72}
    index = Index({"boto3": {"1": 72}})
    row = package_age.rows_for(resolved(("boto3", "1")), settings, NOW, index)[0]
    assert not row.too_young
    younger = package_age.rows_for(resolved(("boto3", "1")), settings, NOW - 1, index)[0]
    assert younger.too_young


def test_the_refusal_says_exactly_this():
    code, state = stage(None, SEVEN)
    assert code == package_age.REFUSED
    assert state["lines"] == [
        "boto3 1.43.125 was published 1h 06m ago.\nbotocore 1.43.125 was published 1h 06m ago.\nThe rule is 72 hours.",
        "Newest that passes: boto3 1.43.121, botocore 1.43.121.\nTo ask Alex: /release orders-service ORD-109 override",
    ]


def test_each_line_stays_whole_in_a_4_5_crop():
    _, state = stage(None, SEVEN)
    for block in state["lines"]:
        for line in block.splitlines():
            assert len(line) <= 54, line


def test_a_refusal_makes_no_change_attachment_or_work_note():
    _, state = stage(None, SEVEN)
    assert "change_attachments" not in state and "change_work_notes" not in state and "force_normal" not in state


def test_a_package_nobody_pinned_counts_too():
    """pip's report, not the file: a young package that only arrives with another is what the file can't show."""
    index = real_index()
    index.packages["young-dep"] = {"1.0": 2}
    pairs = [*OLDER, ("young-dep", "1.0")]
    code, state = stage(None, pairs, index)
    assert code == package_age.REFUSED
    assert "young-dep 1.0 was published 2h 00m ago." in state["lines"][0]
    assert "boto3" not in state["lines"][0]


def test_all_old_enough_passes_with_the_youngest_named(report):
    code, state = stage(None, OLDER)
    assert code == 0
    assert state["lines"] == ["7 packages read from PyPI. The rule is 72 hours.\nYoungest: boto3 1.43.121, 3d 01h old."]
    assert state["change_work_notes"] == [
        "Package age: 7 packages read from PyPI at 20:32 UTC. Youngest: boto3 1.43.121, 3d 01h old. The rule is 72 hours."]
    assert state["change_attachments"] == [str(run_state.REPORTS / package_age.REPORT)]
    body = report()
    assert body["result"] == "passed" and body["rule_hours"] == 72 and body["index"] == package_age.INDEX
    assert [row["package"] for row in body["packages"]][0] in ("boto3", "botocore")
    assert all(row["result"] == "ok" for row in body["packages"])
    assert body["packages"][0]["uploaded"] == "2026-11-03T19:32:04Z"


def test_the_report_keeps_every_package_and_what_passes(report):
    stage(None, SEVEN)
    body = report()
    assert body["result"] == "refused" and body["read_at"] == "2026-11-06T20:32:04Z"
    assert len(body["packages"]) == 7
    assert body["newest_that_passes"] == {"boto3": "1.43.121", "botocore": "1.43.121"}
    young = [row for row in body["packages"] if row["result"] == "too young"]
    assert {row["package"] for row in young} == {"boto3", "botocore"} and young[0]["age"] == "1h 06m"


def test_every_age_is_read_now_and_nothing_is_kept():
    index = real_index()
    stage(None, SEVEN, index)
    first = len([url for url in index.calls if url.endswith("/1.43.125/json")])
    code, state = stage(state_for(), SEVEN, index, now=NOW + 3 * HOUR)
    assert code == package_age.REFUSED and "4h 06m" in state["lines"][0]
    assert len([url for url in index.calls if url.endswith("/1.43.125/json")]) == 2 * first


def test_it_matches_the_exact_file_so_a_wheel_added_later_counts_with_its_own_time():
    index = Index({"boto3": {"1.0": [
        {"filename": "boto3-1.0.tar.gz", "upload_time_iso_8601": stamp(9000), "digests": {"sha256": "the-sdist"}},
        {"filename": "boto3-1.0-py3-none-any.whl", "upload_time_iso_8601": stamp(3), "digests": {"sha256": sha("boto3", "1.0")}},
    ]}})
    row = package_age.rows_for(resolved(("boto3", "1.0")), {"min_hours": 72}, NOW, index)[0]
    assert row.too_young and round(row.age) == 3 * HOUR


# Toward a person.

def test_pypi_not_answering_is_no_reading_and_alex_decides():
    code, state = stage(state_for(), [("not-on-the-fake", "1")])
    assert code == 0
    assert state["force_normal"] is True
    assert state["lines"] == ["Package age: no reading (PyPI didn't answer). Alex decides."]
    assert state["change_details"] == state["lines"]


def test_a_file_pypi_doesnt_list_is_no_reading():
    pairs = resolved(("boto3", "1.43.125"))
    pairs[0] = package_age.Resolved("boto3", "1.43.125", "x.whl", "another-checksum")
    code = package_age.run_stage(state := state_for(), "orders-service", now=NOW, read=real_index(), resolver=lambda *_: pairs)
    assert code == 0 and state["force_normal"] is True
    assert state["lines"] == ["Package age: no reading (PyPI doesn't list that file). Alex decides."]


def test_pip_not_resolving_is_no_reading(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "ERROR: no matching distribution"))
    code = package_age.run_stage(state := state_for(), "orders-service", now=NOW, read=real_index())
    assert code == 0 and state["lines"] == ["Package age: no reading (pip couldn't resolve the packages). Alex decides."]


def test_a_report_it_cant_read_is_no_reading(monkeypatch, tmp_path):
    def wrote(command, **kwargs):
        report = command[command.index("--report") + 1]
        with open(report, "w") as file:
            file.write('{"install": [{"metadata": {}}]}')
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", wrote)
    with pytest.raises(package_age.NoReading):
        package_age.resolve(tmp_path / "requirements.txt", "python3.13")


def test_slack_failing_doesnt_change_the_decision(monkeypatch):
    import notify

    def down(state):
        raise SystemExit("no token")

    monkeypatch.setattr(notify, "post", down)
    code, _ = stage(None, SEVEN)
    assert code == package_age.REFUSED


@pytest.mark.parametrize("no_file", [False, True])
def test_no_third_party_packages_passes(checkout, no_file):
    if no_file:
        (checkout / "requirements.txt").unlink()
    code, state = stage(None, [])
    assert code == 0 and state["lines"] == ["Package age: no third-party packages."]


# What pip is asked.

def test_it_resolves_with_the_builds_own_flags_and_installs_nothing(monkeypatch, tmp_path):
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        report = command[command.index("--report") + 1]
        with open(report, "w") as file:
            json.dump({"install": [{"metadata": {"name": "boto3", "version": "1"},
                                    "download_info": {"url": "https://files.example/boto3-1-py3-none-any.whl",
                                                      "archive_info": {"hashes": {"sha256": "abc"}}}}]}, file)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    items = package_age.resolve(tmp_path / "requirements.txt", "python3.13")
    assert items == [package_age.Resolved("boto3", "1", "boto3-1-py3-none-any.whl", "abc")]
    assert seen["command"][:3] == [sys.executable, "-m", "pip"]
    assert {"--dry-run", "--ignore-installed", "--quiet"} <= set(seen["command"])
    flags = build.wheel_flags("python3.13")
    assert seen["command"][-len(flags):] == flags
    assert flags == ["--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.13", "--only-binary=:all:"]


def test_the_build_still_installs_as_it_did(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: seen.update(command=command, kwargs=kwargs))
    build.install_requirements(tmp_path / "r.txt", tmp_path / "t", "python3.13")
    assert seen["command"] == [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-compile",
                               "--requirement", str(tmp_path / "r.txt"), "--target", str(tmp_path / "t"),
                               "--platform", "manylinux2014_x86_64", "--implementation", "cp",
                               "--python-version", "3.13", "--only-binary=:all:"]
    assert seen["kwargs"] == {"check": True}


# What passes.

@pytest.mark.parametrize("name,fits", [
    ("boto3-1.43.1-py3-none-any.whl", True),
    ("six-1.17.0-py2.py3-none-any.whl", True),
    ("Brotli-1.2.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl", True),
    ("x-1-cp313-cp313-manylinux_2_28_x86_64.whl", False),
    ("x-1-cp313-cp313-manylinux_2_17_aarch64.whl", False),
    ("x-1-cp312-cp312-manylinux2014_x86_64.whl", False),
    ("x-1-cp39-abi3-manylinux2014_x86_64.whl", True),
    ("x-1-cp314-abi3-manylinux2014_x86_64.whl", False),
    ("x-1-cp313-cp313-macosx_11_0_arm64.whl", False),
    ("x-1.tar.gz", False),
])
def test_a_wheel_the_build_could_take(name, fits):
    assert package_age.wheel_fits(name, "python3.13") is fits


def test_newest_that_passes_skips_what_the_build_wouldnt_take():
    index = Index({"boto3": {
        "3.0": 1,
        "2.1rc1": 100,
        "2.0": [{"filename": "boto3-2.0-py3-none-any.whl", "upload_time_iso_8601": stamp(120), "yanked": True, "digests": {"sha256": "y"}}],
        "1.9": [{"filename": "boto3-1.9.tar.gz", "upload_time_iso_8601": stamp(130), "digests": {"sha256": "s"}}],
        "1.8": 140,
    }})
    version, age = package_age.newest_that_passes("boto3", {"min_hours": 72}, "python3.13", NOW, index)
    assert version == "1.8" and round(age) == 140 * HOUR


def test_nothing_old_enough_says_so():
    index = real_index()
    index.packages["boto3"] = {"1": 1}
    index.packages["botocore"] = {"1": 1}
    code, state = stage(None, [("boto3", "1"), ("botocore", "1")], index)
    assert code == package_age.REFUSED
    assert state["lines"][1] == ("No version of boto3, botocore passes yet.\n"
                                 "To ask Alex: /release orders-service ORD-109 override")


# Override: Alex decides, with his reason.

def test_override_asked_goes_to_alex_on_a_normal_change(monkeypatch, report):
    monkeypatch.setattr(package_age, "people", lambda: {"sam": {"slack_user_id": SAM, "display_name": "Sam Developer"}})
    code, state = stage(state_for(option="override"), SEVEN)
    assert code == 0
    assert state["lines"] == [
        "boto3 1.43.125 was published 1h 06m ago.\nbotocore 1.43.125 was published 1h 06m ago.\nThe rule is 72 hours.",
        "Override asked: Alex decides in ServiceNow.",
    ]
    assert state["package_age"]["override"] is True
    assert state["change_details"] == [
        "Package age: boto3 1.43.125 was published 1h 06m ago. botocore 1.43.125 was published 1h 06m ago. The rule is 72 hours.",
        "Source: PyPI's upload times, read at 20:32 UTC, every package in package-age.json.",
        "Override asked by Sam Developer. While a package is younger than the rule, nothing else releases without this approval.",
    ]
    assert state["change_attachments"] == [str(run_state.REPORTS / package_age.REPORT)]
    assert report()["result"] == "override-asked"


def test_override_is_a_normal_change_in_services_yaml():
    from common import service

    config = service("orders-service")
    assert "override" in config["slack"]["options"]
    assert "override" not in config["servicenow"]["standard_options"]


def test_override_with_nothing_young_is_just_a_pass():
    code, state = stage(state_for(option="override"), OLDER)
    assert code == 0 and "override" not in state.get("package_age", {})


class FakeChange:
    REJECTION_COMMENT_LIMIT = 120

    def __init__(self, comment="", broken=False):
        self.comment, self.broken = comment, broken

    def connect(self):
        if self.broken:
            raise SystemExit("ServiceNow is asleep")
        return object(), {}

    def approved_comment(self, sn, change):
        return self.comment


@pytest.fixture
def override_state():
    state = state_for(option="override")
    state["package_age"] = {"override": True, "young": ["boto3 1.43.125, 1h 06m old"]}
    state["change"] = {"number": "CHG1", "sys_id": "s", "approved_by": "Alex"}
    return state


def with_change(monkeypatch, **kwargs):
    monkeypatch.setitem(sys.modules, "change", FakeChange(**kwargs))


def test_an_approval_with_a_reason_goes_on_the_record(monkeypatch, override_state):
    with_change(monkeypatch, comment="Security fix, it can't wait.")
    assert package_age.approved(override_state) == 0
    assert override_state["lines"] == ["Override reason: Security fix, it can't wait."]
    assert override_state["close_notes_extra"] == ("Released with boto3 1.43.125, 1h 06m old, younger than the rule. "
                                                   "Override approved by Alex: Security fix, it can't wait.")


def test_a_long_reason_is_cut_in_slack_and_whole_on_the_change(monkeypatch, override_state):
    comment = "x" * 200
    with_change(monkeypatch, comment=comment)
    assert package_age.approved(override_state) == 0
    assert len(override_state["lines"][0]) <= len("Override reason: ") + 120
    assert comment in override_state["close_notes_extra"]


@pytest.mark.parametrize("kwargs", [{"comment": ""}, {"broken": True}])
def test_no_reason_or_no_way_to_read_it_stops_the_release(monkeypatch, override_state, kwargs):
    with_change(monkeypatch, **kwargs)
    assert package_age.approved(override_state) == package_age.REFUSED
    assert override_state["lines"] == ["Override refused: no reason in Alex's approval."]
    assert "close_notes_extra" not in override_state


def test_a_run_without_override_never_reads_the_approval(monkeypatch):
    with_change(monkeypatch, broken=True)
    assert package_age.approved(state_for()) == 0


# Status writes nothing.

def test_status_prints_the_table_and_writes_nothing():
    lines = []
    assert package_age.status("orders-service", out=lines.append, now=NOW, read=real_index(), resolver=lambda *_: resolved(*SEVEN)) == 0
    text = "\n".join(lines)
    assert "boto3            1.43.125     2026-11-06 19:26  1h 06m   too young" in text
    assert "Decision: refused." in text
    assert not run_state.PATH.exists() and not run_state.REPORTS.exists()


def test_the_table_fits_blue_oceans_pane():
    rows = package_age.rows_for(resolved(*SEVEN), {"min_hours": 72}, NOW, real_index())
    for line in package_age.table(rows, {"min_hours": 72}, NOW).splitlines():
        assert len(line) <= 66, line
