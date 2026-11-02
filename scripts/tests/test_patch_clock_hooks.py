"""What video 24 adds to the shared scripts: scan_line.py on a saved Grype report, publish.py's SBOM
next to the package, and that a run without either gets exactly what it got before.

    .venv/bin/python -m pytest -q scripts/tests/test_patch_clock_hooks.py
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notify  # noqa: E402
import pipeline_calls  # noqa: E402
import publish  # noqa: E402
import run_state  # noqa: E402
import scan_line  # noqa: E402
from test_shared_hooks import FakeJcr, release_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "scripts" / "demos" / "patch_clock" / "tests" / "fixtures"


def saved(name):
    return json.loads((FIXTURES / name).read_text())


def match(vulnerability, severity="Critical", related=(), name="brotli", version="1.2.0", fixed=("1.2.1",), suggested=None):
    return {"vulnerability": {"id": vulnerability, "severity": severity, "fix": {"versions": list(fixed), "state": "fixed" if fixed else "not-fixed"}},
            "relatedVulnerabilities": [{"id": item} for item in related],
            "matchDetails": [{"fix": {"suggestedVersion": suggested}}] if suggested else [{"type": "exact-direct-match"}],
            "artifact": {"name": name, "version": version, "type": "python"}}


def test_the_line_names_the_cve_and_the_fix_from_a_saved_report():
    assert scan_line.line(saved("grype-scan-brotli-1.2.0.json")) == "Scan: critical CVE-2026-111497 in brotli 1.2.0.\nFixed in 1.2.1."


def test_the_cve_comes_from_related_vulnerabilities_when_the_match_is_a_ghsa():
    report = {"matches": [match("GHSA-9v8h-qqgm-4m35", related=["CVE-2026-111497"])]}
    assert scan_line.line(report).startswith("Scan: critical CVE-2026-111497 in brotli 1.2.0.")


def test_a_ghsa_with_no_cve_keeps_its_own_id():
    assert scan_line.line({"matches": [match("GHSA-9v8h-qqgm-4m35")]}).startswith("Scan: critical GHSA-9v8h-qqgm-4m35 in")


def test_several_findings_end_with_how_many_more():
    report = {"matches": [match("CVE-1"), match("CVE-2", name="h11", version="0.14.0"), match("CVE-3", name="h11", version="0.14.0")]}
    assert scan_line.line(report) == "Scan: critical CVE-1 in brotli 1.2.0 and 2 more.\nFixed in 1.2.1."


def test_two_matches_of_one_finding_count_once():
    assert scan_line.line({"matches": [match("CVE-1"), match("GHSA-a", related=["CVE-1"])]}) == "Scan: critical CVE-1 in brotli 1.2.0.\nFixed in 1.2.1."


def test_no_fix_says_so_and_grypes_suggestion_comes_first():
    assert scan_line.line({"matches": [match("CVE-1", fixed=())]}).endswith("\nNo fixed version yet.")
    assert scan_line.line({"matches": [match("CVE-1", fixed=("1.2.1", "2.0.0"), suggested="2.0.0")]}).endswith("\nFixed in 2.0.0.")


def test_only_a_critical_finding_makes_a_line():
    assert scan_line.line({"matches": [match("CVE-1", severity="High")]}) is None
    assert scan_line.line({"matches": []}) is None
    assert scan_line.line(saved("grype-scan-brotli-1.2.1.json")) is None


def test_it_lands_in_the_status_message_above_the_stop_line(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    run_state.save(release_state())
    run_state.REPORTS.mkdir(parents=True)
    (run_state.REPORTS / "grype.json").write_text((FIXTURES / "grype-scan-brotli-1.2.0.json").read_text())
    posted = []
    monkeypatch.setattr(notify, "post", posted.append)
    assert scan_line.main() == 0
    state = posted[0]
    assert state["lines"] == ["Scan: critical CVE-2026-111497 in brotli 1.2.0.\nFixed in 1.2.1."]
    assert "\n\nScan: critical CVE-2026-111497 in brotli 1.2.0.\nFixed in 1.2.1." in notify.render(state)


def test_without_a_report_it_says_nothing_in_slack(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "REPORTS", tmp_path)
    monkeypatch.setattr(notify, "post", lambda state: pytest.fail("no report, no message"))
    assert scan_line.main() == 0


# The pipeline's hooks

PIPELINE = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()


def test_only_a_failed_scan_runs_the_line_and_it_cant_change_the_stage():
    stage = re.search(r"releaseStage\('SBOM and scan'\) \{(.*?)\n            \}\n", PIPELINE, re.S).group(1)
    assert "toolchain.sbomAndScan(service)" in stage
    caught = stage[stage.index("catch (error)"):]
    assert "sh(returnStatus: true, script: 'python3 scripts/scan_line.py')" in caught and "throw error" in caught
    assert "scan_line" not in stage[:stage.index("catch (error)")]


def test_the_clock_stops_in_record_after_the_change_closes_and_cant_fail_it():
    record = PIPELINE[PIPELINE.index("releaseStage('Record')"):PIPELINE.index("} catch (error) {\n            stopped(error)")]
    assert record.index("change.py close successful") < record.index("patchClock.stop()")
    clock = (ROOT / "jenkins" / "shared-library" / "vars" / "patchClock.groovy").read_text()
    assert "sh(returnStatus: true, script: 'python3 scripts/demos/patch_clock/clock.py stop')" in clock
    assert "catch (error)" in clock and "throw" not in clock


def test_the_other_cis_are_still_held_to_jenkins_without_the_lines_only_jenkins_has():
    assert "scan_line.py" not in pipeline_calls.jenkins()
    assert "patchClock" not in " ".join(pipeline_calls.jenkins())
    assert "syft" in pipeline_calls.jenkins() and pipeline_calls.jenkins()[-1] == "change.py close"


def test_the_ci_pull_request_and_test_jobs_never_get_the_line():
    for path in ("jenkins/shared-library/vars/ciPipeline.groovy", "jenkins/pull-requests.Jenkinsfile", "jenkins/jobs/orders-service-tests.Jenkinsfile"):
        assert "scan_line" not in (ROOT / path).read_text(), path


# publish.py

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    package = b"the package of build 8"
    out = tmp_path / "dist" / "orders-service"
    out.mkdir(parents=True)
    info = {"service": "orders-service", "version": "1.12.2", "package": "orders-service-1.12.2.zip",
            "sha256": hashlib.sha256(package).hexdigest()}
    (out / "build-info.json").write_text(json.dumps(info))
    (out / "orders-service-1.12.2.zip").write_bytes(package)
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    monkeypatch.setattr(publish, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(publish, "ssm_client", lambda: None)
    state = release_state()
    state["version"], state["build"]["number"] = "1.12.2", "8"
    state["package"] = {"path": "orders-service-candidates/1.12.2/8/orders-service-1.12.2.zip", "sha256": info["sha256"],
                        "name": "orders-service-1.12.2.zip"}
    run_state.save(state)
    run_state.REPORTS.mkdir(parents=True)
    return package, info


def publish_main(monkeypatch, fake, *args):
    monkeypatch.setattr(publish, "request", fake.request)
    monkeypatch.setattr(sys, "argv", ["publish.py", "orders-service", *args])
    publish.main()


def test_the_sbom_goes_next_to_the_package_tied_to_it_by_checksum(workspace, monkeypatch):
    package, info = workspace
    (run_state.REPORTS / "sbom.cdx.json").write_bytes(b'{"components": []}')
    fake = FakeJcr({})
    publish_main(monkeypatch, fake)
    base = "orders-service-candidates/1.12.2/8"
    assert fake.puts == [f"{base}/orders-service-1.12.2.zip", f"{base}/build-info.json", f"{base}/sbom.cdx.json", f"{base}/sbom-info.json"]
    assert fake.files[f"{base}/sbom.cdx.json"] == b'{"components": []}'
    tied = json.loads(fake.files[f"{base}/sbom-info.json"])
    assert tied == {"service": "orders-service", "version": "1.12.2", "package_sha256": info["sha256"],
                    "sbom_sha256": hashlib.sha256(b'{"components": []}').hexdigest()}


def test_a_run_without_an_sbom_uploads_what_it_always_did(workspace, monkeypatch):
    fake = FakeJcr({})
    publish_main(monkeypatch, fake)
    base = "orders-service-candidates/1.12.2/8"
    assert fake.puts == [f"{base}/orders-service-1.12.2.zip", f"{base}/build-info.json"]


def candidate_files(package):
    base = "orders-service-candidates/1.12.2/8"
    return {f"{base}/orders-service-1.12.2.zip": package, f"{base}/sbom.cdx.json": b"the sbom", f"{base}/sbom-info.json": b"the tie"}


def test_a_release_copies_the_sbom_next_to_the_release_copy(workspace, monkeypatch):
    package, _ = workspace
    fake = FakeJcr(candidate_files(package))
    publish_main(monkeypatch, fake, "--release")
    base = "orders-service-releases/1.12.2"
    assert fake.puts == [f"{base}/orders-service-1.12.2.zip", f"{base}/build-info.json", f"{base}/sbom.cdx.json", f"{base}/sbom-info.json"]
    assert fake.files[f"{base}/sbom.cdx.json"] == b"the sbom"


def test_another_build_keeps_its_sbom_beside_its_own_package(workspace, monkeypatch):
    package, _ = workspace
    fake = FakeJcr({**candidate_files(package), "orders-service-releases/1.12.2/orders-service-1.12.2.zip": b"build 7's package",
                    "orders-service-releases/1.12.2/sbom.cdx.json": b"build 7's sbom"})
    publish_main(monkeypatch, fake, "--release")
    assert fake.files["orders-service-releases/1.12.2/8/sbom.cdx.json"] == b"the sbom"
    assert fake.files["orders-service-releases/1.12.2/sbom.cdx.json"] == b"build 7's sbom"


def test_a_release_folder_gets_the_sbom_it_lacked_when_the_same_package_is_released_again(workspace, monkeypatch):
    package, _ = workspace
    fake = FakeJcr({**candidate_files(package), "orders-service-releases/1.12.2/orders-service-1.12.2.zip": package})
    publish_main(monkeypatch, fake, "--release")
    assert fake.puts == ["orders-service-releases/1.12.2/sbom.cdx.json", "orders-service-releases/1.12.2/sbom-info.json"]


def test_a_kubernetes_build_infos_image_is_a_string_and_its_digest_has_its_own_key():
    """catalog's build-info.json: "image" is "repository@digest". Its first release after the merge stopped here. 2 November."""
    tied = publish.sbom_info({"service": "catalog", "version": "1.10.0", "sha256": "ab",
                              "image": "registry/catalog@sha256:cd", "image_digest": "sha256:cd"}, b"{}")
    assert tied["image_digest"] == "sha256:cd" and tied["service"] == "catalog"


def test_an_image_service_ties_its_digest_too():
    tied = publish.sbom_info({"service": "quote-service", "version": "1.2.0", "sha256": "ab", "image": {"digest": "sha256:cd"}}, b"x")
    assert tied["image_digest"] == "sha256:cd" and tied["package_sha256"] == "ab"
