"""Video 11: the auditor's sample, end to end on recorded fixtures: the
population and what it leaves out, the draw, each of the five answers in
each form, the pages, the findings hash and the ZIP.

    .venv/bin/python -m pytest -q scripts/demos/auditor/tests
"""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import evidence_sample
import fixtures
import records
import sample


class Args(argparse.Namespace):
    def __init__(self, seed=4127, size=10, dry_run=False):
        super().__init__(start=datetime.date(2026, 10, 27), end=datetime.date(2026, 10, 29), size=size, seed=seed,
                         dry_run=dry_run, out=None)


def reader_aws(cloudtrail=None, lambda_=None):
    aws = records.AwsReader.__new__(records.AwsReader)
    aws.cloudtrail, aws.lambda_ = cloudtrail or fixtures.FakeCloudTrail(), lambda_ or fixtures.FakeLambda()
    aws.function, aws.sleep, aws.read = "orders-service-production", lambda s: None, {}
    return aws


@pytest.fixture
def run(tmp_path, monkeypatch):
    def go(seed=4127, size=10, lambda_wrong=(), jfrog_wrong=(), name="out", dry_run=False):
        sn = fixtures.FakeSN()
        monkeypatch.setattr(records.ServiceNowReader, "download", lambda self, sys_id: sn.files[sys_id])
        aws = reader_aws(lambda_=fixtures.FakeLambda(lambda_wrong))
        out = tmp_path / name
        code = evidence_sample.run(Args(seed, size, dry_run), sn=sn, aws=aws, jfrog=fixtures.FakeJFrog(jfrog_wrong), out=out)
        return code, out, sn, aws

    return go


def rows_of(out):
    return {item["change"]: item for item in json.loads((out / "findings.json").read_text())}


def test_the_population_holds_closed_changes_that_moved_production(run, capsys):
    code, _, _, _ = run(dry_run=True, seed=None)
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == "orders-service: 7 changes reached production, 2026-10-27 to 2026-10-29."
    assert "Left out: 1 cancelled, 1 not closed, 1 closed without a production move." in out
    for number in ("CHG0030030", "CHG0030034", "CHG0030041", "CHG0030050"):
        assert number in out
    assert "CHG0030048" not in out and "CHG0030049" not in out and "CHG0030051" not in out


def test_the_draw_follows_the_printed_rule(run):
    changes = fixtures.changes()
    included, _ = sample.population(changes, reader_aws().alias_moves(fixtures.when(26, 0, 0), fixtures.when(31, 0, 0)))
    numbers = [records.value(c, "number") for c, _ in included]
    by_hand = sorted(numbers, key=lambda n: hashlib.sha256(f"4127:{n}".encode()).hexdigest())[:3]
    drawn = [records.value(c, "number") for c, _ in sample.draw(included, 3, 4127)]
    assert drawn == sorted(by_hand)
    assert drawn == [records.value(c, "number") for c, _ in sample.draw(included, 3, 4127)]
    assert drawn != [records.value(c, "number") for c, _ in sample.draw(included, 3, 99)]


def test_the_same_seed_and_period_give_the_same_findings_hash(run):
    _, first, _, _ = run(name="first")
    _, second, _, _ = run(name="second")
    _, other, _, _ = run(seed=99, size=3, name="other")
    assert (first / "findings.json").read_bytes() == (second / "findings.json").read_bytes()
    assert (first / "description.txt").read_text() == (second / "description.txt").read_text()
    assert (first / "evidence-4127.zip").read_bytes() == (second / "evidence-4127.zip").read_bytes()
    assert (first / "findings.json").read_bytes() != (other / "findings.json").read_bytes()
    found = hashlib.sha256((first / "findings.json").read_bytes()).hexdigest()
    assert (first / "description.txt").read_text().strip() == f"Sample 7 of 7, seed 4127, findings {found[:8]}"


def test_a_normal_change_approved_by_alex_meets_all_five(run):
    _, out, _, _ = run()
    row = rows_of(out)["CHG0030041"]
    assert row["answers"] == ["yes"] * 5 and row["result"] == "Live" and row["version"] == "1.4.0"
    page = (out / "evidence" / "CHG0030041.html").read_text()
    sha = fixtures.package_sha("CHG0030041")
    assert page.count(sha) == 3, "the change, JFrog and AWS, side by side"
    assert "In AWS (orders-service-production version 30, CodeSha256)" in page
    assert "Approved by Alex Change Manager: 2026-10-28 13:05:00 UTC (approval record)" in page
    assert "Production live alias moved to version 30: 2026-10-28 13:09:00 UTC (CloudTrail, UpdateAlias)" in page
    assert '<a href="https://servicenow.example/sysapproval_approver.do?sys_id=approval-CHG0030041">Approval record</a>' in page


def test_standard_changes_are_pre_approved_and_rollbacks_are_by_design(run):
    _, out, _, _ = run()
    rows = rows_of(out)
    assert rows["CHG0030030"]["answers"] == ["pre-approved", "pre-approved", "yes", "yes", "yes"]
    assert rows["CHG0030034"]["result"] == "Rolled back to 1.2.3"
    assert rows["CHG0030050"]["answers"] == ["pre-approved", "by design", "by design", "by design", "by design"]
    assert rows["CHG0030050"]["result"] == "Rollback live: 1.4.0"
    rollback = (out / "evidence" / "CHG0030050.html").read_text()
    assert "CHG0030047, the release it went back to" in rollback, "the latest closed release of 1.4.0 before it"
    standard = (out / "evidence" / "CHG0030030.html").read_text()
    assert "Standard change template: Routine release of orders-service" in standard
    index = (out / "evidence-sample.html").read_text()
    assert 'pre-approved<span class="under">Routine release of orders-service</span>' in index


def test_exceptions_say_why(run):
    _, out, _, _ = run(jfrog_wrong={"CHG0030047"})
    rows = rows_of(out)
    assert rows["CHG0030045"]["answers"][1] == "no: can't tell from the record"
    assert rows["CHG0030046"]["answers"][0] == "no"
    assert rows["CHG0030047"]["answers"][2] == "no"
    assert "last updated after the deploy" in (out / "evidence" / "CHG0030045.html").read_text()
    assert "Approved by the requester, Sam Developer." in (out / "evidence" / "CHG0030046.html").read_text()
    index = (out / "evidence-sample.html").read_text()
    assert "Meets all five: 4 of 7 (3 pre-approved by template). Exceptions: 3." in index


def test_lambda_s_code_hash_must_match_too(run):
    _, out, _, _ = run(lambda_wrong={"CHG0030041"})
    assert rows_of(out)["CHG0030041"]["answers"][2] == "no"


def test_tests_and_scan_answers_read_the_attachments():
    files = fixtures.attachments_for("CHG0030041", "1.4.0", "57")
    assert sample.answer_tests(files).text == "yes"
    assert sample.answer_tests({**files, "junit.xml": fixtures.junit(failures=1)}).text == "no"
    assert sample.answer_tests({}).lines == ["No junit.xml attached."]
    critical = json.dumps({"matches": [{"vulnerability": {"severity": "Critical"}}]}).encode()
    assert sample.answer_scan({**files, "grype.json": critical}).text == "no"
    assert sample.answer_scan(files).lines == ["sbom.cdx.json: 7 components",
                                               "grype.json: critical 0, high 0, medium 1, low 0"]


def test_cloudtrail_is_read_in_one_paged_lookup(run):
    _, _, _, aws = run()
    assert aws.cloudtrail.calls == 2, "two pages of one lookup"


def test_the_pages_render_under_jenkins_content_security_policy(run):
    _, out, _, _ = run()
    pages = [out / "evidence-sample.html", *sorted((out / "evidence").glob("*.html"))]
    assert sorted(p.name for p in (out / "evidence").iterdir() if not p.name.endswith(".html")) == ["style.css"]
    for path in pages:
        text = path.read_text()
        assert "<script" not in text and " style=" not in text and "<style" not in text
        assert "target=" not in text, "links open in the same tab"
        assert re.search(r'<link rel="stylesheet" href="(evidence/)?style.css">', text)
        for href, label in re.findall(r'<a href="([^"]+)">([^<]+)</a>', text):
            assert "servicenow.example" not in label and "jenkins-demo" not in label


def test_the_report_names_no_law_or_standard_and_claims_nothing():
    words = re.compile(r"audit-ready|compliant|compliance|\bSOX\b|Sarbanes|ISO ?27001|SOC ?2|\bDORA\b|NIS2|GDPR|PCI|HIPAA",
                       re.I)
    here = Path(sample.__file__).parent
    for name in ("report.py", "sample.py", "evidence_sample.py"):
        assert not words.search((here / name).read_text()), name


def test_the_zip_holds_the_records_without_values_keys_or_addresses(run, tmp_path):
    _, out, _, _ = run()
    folder = tmp_path / "unzipped"
    with zipfile.ZipFile(out / "evidence-4127.zip") as archive:
        archive.extractall(folder)
        names = archive.namelist()
    assert "attachments/CHG0030041/build-info.json" in names and "servicenow/approvals.json" in names
    assert "cloudtrail/update-alias-events.json" in names and any(n.startswith("jfrog/") for n in names)
    text = "\n".join(p.read_text(errors="replace") for p in folder.rglob("*") if p.is_file())
    assert '"DELIVERY_DAYS": "(value removed)"' in text and '"2"' not in text.split("Environment")[1][:200]
    assert "198.51.100.7" not in text and "ASIAEXAMPLE" not in text
    if shutil.which("gitleaks"):
        scan = subprocess.run(["gitleaks", "dir", str(folder), "--no-banner"], capture_output=True, text=True)
        assert scan.returncode == 0, scan.stdout


def test_the_period_is_amsterdam_calendar_days():
    start, end = evidence_sample.period(datetime.date(2026, 10, 27), datetime.date(2026, 10, 29))
    assert start == datetime.datetime(2026, 10, 26, 23, 0, tzinfo=datetime.timezone.utc)
    assert end == datetime.datetime(2026, 10, 29, 23, 0, tzinfo=datetime.timezone.utc)
    assert evidence_sample.default_dates(datetime.date(2026, 11, 2)) == (datetime.date(2026, 10, 26),
                                                                          datetime.date(2026, 11, 2))


def test_a_seed_is_required_for_a_sample():
    with pytest.raises(SystemExit):
        evidence_sample.parse(["--from", "2026-10-27", "--to", "2026-10-29"])
    assert evidence_sample.parse(["--dry-run"]).seed is None


# The read-only identities

ROOT = Path(evidence_sample.ROOT)


def test_ines_may_start_this_one_job_and_read_nothing_else():
    import yaml

    casc = yaml.safe_load((ROOT / "jenkins" / "casc.yaml").read_text())
    entries = casc["jenkins"]["authorizationStrategy"]["projectMatrix"]["entries"]
    by_user = {entry["user"]["name"]: entry["user"]["permissions"] for entry in entries}
    assert by_user["ines"] == ["Overall/Read"]
    assert by_user["admin"] == ["Overall/Administer"] and "Job/Build" in by_user["release-bot"]
    assert {"id": "ines", "name": "Ines, auditor", "password": "${jenkins-ines-password}"} in \
        casc["jenkins"]["securityRealm"]["local"]["users"]
    jobs = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    block = jobs.split("pipelineJob('evidence-sample')")[1].split("\npipelineJob(")[0]
    assert "name('ines')" in block and "permissions(['Job/Build', 'Job/Read'])" in block
    assert jobs.count("name('ines')") == 1, "no other job names her"


def test_ines_never_has_an_empty_password(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("host_secrets", ROOT / "tools-host" / "host_secrets.py")
    host_secrets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host_secrets)
    assert "servicenow/ines-password" in host_secrets.NOT_ON_HOST
    assert host_secrets.RANDOM_UNTIL_SET == {"jenkins/ines-password"}


def test_servicenow_s_readers_are_read_only_and_approve_nothing():
    import servicenow_setup

    ines, reader = servicenow_setup.PEOPLE["ines.auditor"], servicenow_setup.PEOPLE["evidence.reader"]
    assert "snc_read_only" in ines["roles"] and "snc_read_only" in reader["roles"]
    assert not ines.get("machine") and reader.get("machine") and reader.get("generated")
    assert "approver_user" not in ines["roles"] + reader["roles"]
    for group in servicenow_setup.GROUPS.values():
        assert "ines.auditor" not in group["members"] and "evidence.reader" not in group["members"]


def test_the_evidence_reader_s_jfrog_token_is_stored_by_jcr_setup():
    import jcr_setup

    assert jcr_setup.READER == "evidence-reader"
    assert any("Read only" in step and "Deploy/Cache" in step for step in jcr_setup.READER_STEPS)
    tools_host = (ROOT / "infra" / "tools_host.tf").read_text()
    assert "parameter/release-demo/jcr/evidence-reader-token" in tools_host
