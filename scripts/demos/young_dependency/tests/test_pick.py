"""Which package on the day: nothing here makes one young, and when nothing qualifies the take waits."""

import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import package_age
import pick
from conftest import HOUR, NOW, OLDER, SEVEN, Index, real_index, resolved

BASE = """# The AWS SDK the page's deploy time calls, pinned with everything it brings.
boto3==1.43.124
botocore==1.43.124
jmespath==1.1.0
python-dateutil==2.9.0.post0
s3transfer==0.19.3
six==1.17.0
urllib3==2.8.0
"""
RULES = {"min_hours": 72}
PYTHON = "python3.13"


def from_text(text):
    """A fake pip: what these pins would install."""
    return [line.split("==") for line in text.splitlines() if line and not line.startswith("#")]


def resolver(path, runtime):
    return resolved(*[tuple(pair) for pair in from_text(Path(path).read_text())])


def all_index(**packages):
    """Everything choose() reads: the seven, and every candidate old enough unless given."""
    index = real_index()
    for candidate in pick.WATCH[1:]:
        index.packages[candidate.package] = {"9.0": 4000, "8.0": 5000}
    for name, versions in packages.items():
        index.packages[name] = versions
    return index


def none_found(package, version):
    return []


# One candidate.

def test_survey_reads_newest_and_what_passes():
    found = pick.survey(pick.WATCH[0], RULES, PYTHON, NOW, real_index())
    assert (found.newest, found.older, found.why_not) == ("1.43.125", "1.43.121", None)
    assert round(found.newest_age) == round(1.1 * HOUR) and round(found.older_age) == 73 * HOUR


@pytest.mark.parametrize("hours,good", [(67.9, True), (68, False), (71.9, False), (80, False)])
def test_the_margin_keeps_the_refusal_alive_through_the_take(hours, good):
    index = Index({"filelock": {"2": hours, "1": 500}})
    found = pick.survey(pick.Candidate("filelock"), RULES, PYTHON, NOW, index)
    assert (found.why_not is None) == good


def test_a_package_with_no_version_old_enough_wont_do():
    found = pick.survey(pick.Candidate("filelock"), RULES, PYTHON, NOW, Index({"filelock": {"1": 3}}))
    assert found.why_not == "no version old enough to fall back on"


def test_its_partner_must_have_the_same_versions():
    index = real_index()
    del index.packages["botocore"]["1.43.125"]
    found = pick.survey(pick.WATCH[0], RULES, PYTHON, NOW, index)
    assert found.why_not == "botocore has no matching version"


# The pins.

def test_rewrite_moves_only_what_it_is_told():
    text = pick.rewrite(BASE, {"boto3": "1.43.125", "botocore": "1.43.125"})
    assert text.splitlines()[0] == BASE.splitlines()[0]
    assert "boto3==1.43.125\nbotocore==1.43.125\njmespath==1.1.0" in text
    assert sum(left != right for left, right in zip(BASE.splitlines(), text.splitlines())) == 2


def test_rewrite_marks_a_package_nothing_imports():
    text = pick.rewrite(BASE, {"filelock": "4.2.9"})
    assert text.endswith(f"urllib3==2.8.0\n{pick.DECLARED}\nfilelock==4.2.9\n")


def test_the_sdk_itself_when_its_young():
    found = pick.survey(pick.WATCH[0], RULES, PYTHON, NOW, real_index())
    text = pick.refused_pins(found, BASE, found)
    assert "boto3==1.43.125\nbotocore==1.43.125" in text


def test_a_fallback_leaves_the_sdk_older_than_the_rule():
    index = all_index(filelock={"4.2.9": 5, "4.2.6": 100})
    primary = pick.survey(pick.WATCH[0], RULES, PYTHON, NOW, index)
    found = pick.survey(pick.WATCH[1], RULES, PYTHON, NOW, index)
    text = pick.refused_pins(found, BASE, primary)
    assert "boto3==1.43.121\nbotocore==1.43.121" in text and text.endswith("filelock==4.2.9\n")


def test_passing_pins_move_only_the_young_ones_to_what_slack_names():
    text = pick.rewrite(BASE, {"boto3": "1.43.125", "botocore": "1.43.125"})
    after, moved = pick.passing_pins(text, RULES, PYTHON, NOW, resolver, real_index())
    assert moved == {"boto3": "1.43.121", "botocore": "1.43.121"}
    named = package_age.newest_that_passes("boto3", RULES, PYTHON, NOW, real_index())[0]
    assert f"boto3=={named}\n" in after
    assert sum(left != right for left, right in zip(text.splitlines(), after.splitlines())) == 2


def test_passing_pins_with_nothing_young_changes_nothing():
    text = pick.rewrite(BASE, {"boto3": "1.43.121", "botocore": "1.43.121"})
    after, moved = pick.passing_pins(text, RULES, PYTHON, NOW, resolver, real_index())
    assert after == text and moved == {}


# The choice.

def test_boto3_first_when_its_young():
    lines = []
    chosen, _ = pick.choose(BASE, NOW, all_index(filelock={"4.2.9": 5, "4.2.6": 100}), resolver, none_found, lines.append)
    assert chosen.candidate.package == "boto3" and chosen.candidate.imported
    assert "chosen" in lines[0]
    assert "not tried: an earlier one qualified" in lines[1]


def old_sdk():
    sdk = {"1.43.124": 100, "1.43.123": 130}
    return {"boto3": dict(sdk), "botocore": dict(sdk)}


def test_a_small_package_when_boto3_hasnt_released():
    chosen, primary = pick.choose(BASE, NOW, all_index(filelock={"4.2.9": 5, "4.2.6": 100}, **old_sdk()), resolver, none_found, lambda line: None)
    assert chosen.candidate.package == "filelock" and not chosen.candidate.imported
    text = pick.refused_pins(chosen, BASE, primary)
    assert "boto3==1.43.124\nbotocore==1.43.124" in text and "filelock==4.2.9" in text


def test_nothing_qualifying_chooses_nothing():
    chosen, _ = pick.choose(BASE, NOW, all_index(**old_sdk()), resolver, none_found, lambda line: None)
    assert chosen is None


def test_a_finding_on_the_young_version_rules_it_out():
    """The hook says nothing is wrong with it."""
    def findings(package, version):
        return ["GHSA-xxxx"] if package == "boto3" else []

    lines = []
    chosen, _ = pick.choose(BASE, NOW, all_index(filelock={"4.2.9": 5, "4.2.6": 100}), resolver, findings, lines.append)
    assert chosen.candidate.package == "filelock"
    assert "has findings on OSV: GHSA-xxxx" in lines[0]


def test_osv_down_doesnt_rule_it_out_but_says_look(monkeypatch, capsys):
    def down(*args, **kwargs):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert pick.osv_findings("boto3", "1") is None
    assert "look by hand" in capsys.readouterr().out


def test_pins_that_dont_do_what_the_take_needs_are_ruled_out():
    def extra_young(path, runtime):
        return [*resolver(path, runtime), *resolved(("newcomer", "1"))]

    index = all_index(newcomer={"1": 2})
    lines = []
    chosen, _ = pick.choose(BASE, NOW, index, extra_young, none_found, lines.append)
    assert chosen is None
    assert "under these pins the young ones are" in lines[0]


# What it tells you.

def test_show_says_how_long_it_holds_and_what_pypi_shows():
    lines = []
    assert pick.show(NOW, all_index(), resolver, none_found, lines.append) == 0
    text = "\n".join(lines)
    assert "Chosen: boto3 1.43.125 and botocore 1.43.125, 1h 06m old. Newest that passes: 1.43.121, 3d 01h old." in text
    assert "The handler imports it." in text
    assert "Under the rule until Mon 09 Nov 20:26 Amsterdam. Take it before Mon 09 Nov 16:26." in text
    assert "PyPI's page says \"about N hours ago\" until 01:00 Amsterdam, then the date." in text


def test_show_a_day_later_says_pypi_shows_the_date():
    lines = []
    assert pick.show(NOW + 6 * HOUR, all_index(), resolver, none_found, lines.append) == 0
    assert "PyPI's page shows the date, not the hours" in "\n".join(lines)


def test_show_says_the_fallback_is_declared():
    lines = []
    assert pick.show(NOW, all_index(filelock={"4.2.9": 5, "4.2.6": 100}, **old_sdk()), resolver, none_found, lines.append) == 0
    assert "Declared, not imported: use the script's fallback lines" in "\n".join(lines)


def test_show_with_nothing_exits_1_and_says_the_take_waits():
    lines = []
    assert pick.show(NOW, all_index(**old_sdk()), resolver, none_found, lines.append) == 1
    assert "The take waits" in lines[-1]


# On main.

class Repo:
    """A fresh clone's service folder and demo states, and the git and stage.py a write would run."""

    def __init__(self, root, monkeypatch, on_main="1.32.0", pins=BASE):
        self.root, self.git_calls = root, []
        self.folder = root / "services" / "orders-service"
        self.folder.mkdir(parents=True)
        (self.folder / "VERSION").write_text(on_main + "\n")
        (self.folder / "requirements.txt").write_text(pins)
        states = root / "demo-states" / "orders-service"
        for version in ("1.32.0", "1.32.1"):
            (states / version).mkdir(parents=True)
            (states / version / "VERSION").write_text(version + "\n")
            (states / version / "requirements.txt").write_text(BASE)
        config = {"path": "services/orders-service", "dir": self.folder, "runtime": PYTHON,
                  "package_age": {"min_hours": 72, "versions": ["1.32.0", "1.32.1"]}}
        monkeypatch.setattr(pick, "ROOT", root)
        monkeypatch.setattr(pick, "service", lambda name: config)
        monkeypatch.setattr(pick, "git", self.git)

        def stage_py(command, **kwargs):
            version = command[3]
            self.staged = version
            for name in ("VERSION", "requirements.txt"):
                (self.folder / name).write_text((states / version / name).read_text())
            return subprocess.CompletedProcess(command, 0, f"Staged orders-service {version}", "")

        monkeypatch.setattr(pick.subprocess, "run", stage_py)

    def git(self, *args, check=True):
        self.git_calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:2] == ("diff", "--cached") else 0, "", "")

    def pins(self):
        return (self.folder / "requirements.txt").read_text()

    def commit_message(self):
        return next(args[args.index("-m") + 1] for args in self.git_calls if args[0] == "commit")


def test_write_1_32_0_stages_todays_newest_in_one_commit(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch, on_main="1.3.0")
    pick.write("1.32.0", NOW, all_index(), resolver, none_found)
    assert repo.staged == "1.32.0"
    assert "boto3==1.43.125\nbotocore==1.43.125" in repo.pins()
    assert repo.commit_message() == "demo: stage orders-service 1.32.0, boto3 1.43.125 [skip ci]"
    assert ("push", "--quiet", "origin", "HEAD:main") in repo.git_calls


def test_write_1_32_1_moves_the_young_pins_on_main_to_what_passes(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch, pins=pick.rewrite(BASE, {"boto3": "1.43.125", "botocore": "1.43.125"}))
    pick.write("1.32.1", NOW, all_index(), resolver, none_found)
    assert repo.staged == "1.32.1"
    assert "boto3==1.43.121\nbotocore==1.43.121" in repo.pins()
    assert repo.commit_message() == "demo: stage orders-service 1.32.1, boto3 1.43.121, botocore 1.43.121 [skip ci]"


def test_write_1_32_1_needs_1_32_0_on_main(tmp_path, monkeypatch):
    Repo(tmp_path, monkeypatch, on_main="1.3.0")
    with pytest.raises(SystemExit):
        pick.write("1.32.1", NOW, all_index(), resolver, none_found)


def test_nothing_qualifying_stages_nothing(tmp_path, monkeypatch):
    repo = Repo(tmp_path, monkeypatch, on_main="1.3.0")
    with pytest.raises(SystemExit):
        pick.write("1.32.0", NOW, all_index(**old_sdk()), resolver, none_found)
    assert not hasattr(repo, "staged") and not repo.git_calls


def test_only_its_two_versions():
    with pytest.raises(SystemExit):
        pick.main(["write", "1.4.0"])


def test_stage_runs_this_file_in_the_fresh_clone(monkeypatch):
    import host

    seen = {}
    monkeypatch.setattr(host, "stage", lambda service, version, wrapper=None: seen.update(service=service, version=version, wrapper=wrapper))
    pick.stage("1.32.0")
    assert seen == {"service": "orders-service", "version": "1.32.0", "wrapper": [pick.THIS_FILE, "write", "1.32.0"]}
    assert (pick.ROOT / pick.THIS_FILE).exists()


def test_the_seven_and_the_older_pins_agree_with_the_fixture():
    assert [pair[0] for pair in SEVEN] == [pair[0] for pair in OLDER]
