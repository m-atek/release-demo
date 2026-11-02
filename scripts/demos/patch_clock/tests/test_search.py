"""The search: the reply's text from saved Grype JSON, and that a failed read, a missing SBOM and a
checksum mismatch are never clear. AWS, JFrog and Grype are fakes.

    .venv/bin/python -m pytest -q scripts/demos/patch_clock/tests
"""

import ast
import html
import io
import json
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import clock
import live as readers
import search
import tools
from conftest import SAM, HERE, b64digest, hexdigest, released, saved, saved_bytes

ROOT = HERE.parents[2]
NOT_SEARCHED = "Not searched: catalog, inventory, runbook-assistant, store-assistant, store-floor, store-sync."
REQUESTED = 1793000000.0


def searched(vulnerability="CVE-2026-111497", asked_by=SAM):
    """The reply as Slack shows it."""
    return html.unescape(search.search(vulnerability, asked_by, REQUESTED))


@pytest.fixture
def six_not_searched(monkeypatch):
    monkeypatch.setattr(readers, "not_searched", lambda: ["catalog", "inventory", "runbook-assistant", "store-assistant", "store-floor", "store-sync"])


def test_the_first_reply_reads_as_the_spec_writes_it(world, six_not_searched):
    assert searched() == f"""*CVE-2026-111497* · Critical · known exploited · asked by <@{SAM}>
brotli >=1.0.3,<1.2.1 · fixed in 1.2.1 · published 22 Oct 2026

Searched the SBOMs of 3 live services in 31s:
:x:  orders-service 1.12.0 holds brotli 1.2.0
:white_check_mark:  pricing-api 2.1.2: clear, 0 components
:white_check_mark:  quote-service 1.2.0: clear, 30 components

1 of 3 holds it.

{NOT_SEARCHED}"""


def test_slack_gets_the_range_escaped_and_the_mention_as_it_is(world):
    sent = search.search("CVE-2026-111497", SAM, REQUESTED)
    assert "\nbrotli &gt;=1.0.3,&lt;1.2.1 · fixed in 1.2.1 · published 22 Oct 2026\n" in sent
    assert f"asked by <@{SAM}>\n" in sent


def test_a_range_that_could_read_as_a_link_goes_out_escaped():
    facts = {"severity": "Critical", "known_exploited": False, "published": None, "names": {"a"}, "ids": {"CVE-1"},
             "packages": [{"name": "a", "range": ["<1.0", ">=2.0,<2.1"], "fixed": ["1.0", "2.1"], "unfixed": False}]}
    assert search.advisory_line(facts, []) == "a <1.0 || >=2.0,<2.1 · fixed in 1.0 or 2.1"
    assert "\na &lt;1.0 || &gt;=2.0,&lt;2.1 · fixed in 1.0 or 2.1\n" in search.reply("CVE-1", facts, SAM, [], 1, [])


def test_a_clear_row_counts_packages_not_the_files_they_own():
    sbom = {"components": [{"type": "library", "name": "a"}, {"type": "operating-system", "name": "alpine"}, {"type": "file", "name": "/bin/sh"}]}
    row = search.verdict("quote-service", "1.2.0", {"matches": []}, sbom, {"ids": {"CVE-1"}, "names": {"brotli"}})
    assert row.kind == search.CLEAR and row.says == "quote-service 1.2.0: clear, 2 components"


def test_the_second_reply_names_the_patched_package_and_says_0_of_3(world, six_not_searched):
    world.patched()
    text = searched()
    assert ":white_check_mark:  orders-service 1.12.2: clear, brotli 1.2.1\n" in text
    assert "\n\n0 of 3 hold it.\n\n" in text
    assert ":x:" not in text and "brotli >=1.0.3,<1.2.1 · fixed in 1.2.1" in text


def test_the_id_nothing_holds_answers_0_of_n(world, monkeypatch):
    monkeypatch.setattr(tools, "db_search", lambda *ids: [{"vulnerability": {"id": "GHSA-www2-v7xj-xrc6", "aliases": ["CVE-2018-20060"], "severity": "critical",
                                                                              "provider": "github", "published_date": "2018-12-12T00:00:00Z"},
                                                           "packages": [{"package": {"name": "urllib3", "ecosystem": "python"},
                                                                         "detail": {"ranges": [{"version": {"constraint": "<1.23"}, "fix": {"version": "1.23", "state": "fixed"}}]}}]}])
    world.scan = lambda sbom_file: saved("grype-by-cve-clear.json")
    monkeypatch.setattr(tools, "scan", world.scan)
    text = searched("CVE-2018-20060")
    assert "*CVE-2018-20060* · Critical · asked by" in text and "known exploited" not in text
    assert "urllib3 <1.23 · fixed in 1.23 · published 12 Dec 2018" in text
    assert "0 of 3 hold it." in text


def test_grypes_scan_with_another_advisorys_match_doesnt_count(world, monkeypatch):
    other = saved("grype-by-cve-brotli-1.2.0.json")
    other["matches"][0]["vulnerability"]["id"] = "CVE-2020-1"
    other["matches"][0]["relatedVulnerabilities"] = []
    monkeypatch.setattr(tools, "scan", lambda sbom_file: other)
    assert "0 of 3 hold it." in searched()


def test_a_match_counts_through_its_related_id_too(world, monkeypatch):
    as_the_pipeline_scans = saved("grype-scan-brotli-1.2.0.json")  # the match's id is the GHSA
    monkeypatch.setattr(tools, "scan", lambda sbom_file: as_the_pipeline_scans)
    monkeypatch.setattr(search, "ghsa_ids", lambda cve, status: [])
    monkeypatch.setattr(tools, "db_search", lambda *ids: saved("grype-db-search-nvd-only.json"))
    assert "3 of 3 hold it." in searched()


def test_a_ghsa_query_finds_the_same_and_shows_its_own_id(world):
    text = searched("GHSA-9v8h-qqgm-4m35")
    assert text.startswith(f"*GHSA-9v8h-qqgm-4m35* · Critical · known exploited · asked by <@{SAM}>\n")
    assert "1 of 3 holds it." in text


def test_an_id_the_database_doesnt_know_says_so_and_searches_nothing(world, monkeypatch):
    monkeypatch.setattr(tools, "db_search", lambda *ids: [])
    monkeypatch.setattr(search, "ghsa_ids", lambda cve, status: [])
    text = searched("CVE-2099-12345")
    assert text == f"*CVE-2099-12345* · asked by <@{SAM}>\n\nGrype's database doesn't know CVE-2099-12345. Nothing searched."
    assert world.scans == [] and world.functions.calls == []


def test_a_record_with_no_affected_package_is_still_known(world, monkeypatch):
    monkeypatch.setattr(tools, "db_search", lambda *ids: [])
    monkeypatch.setattr(tools, "db_records", lambda vulnerability: saved("grype-db-search-vuln-nvd.json"))
    monkeypatch.setattr(search, "ghsa_ids", lambda cve, status: [])
    assert searched().split("\n")[1] == "published 22 Oct 2026"


def test_without_githubs_record_the_facts_come_from_what_the_database_has(world, monkeypatch):
    monkeypatch.setattr(tools, "db_search", lambda *ids: saved("grype-db-search-nvd-only.json"))
    monkeypatch.setattr(search, "ghsa_ids", lambda cve, status: [])
    head, second = searched().split("\n")[:2]
    assert head == f"*CVE-2026-111497* · Critical · known exploited · asked by <@{SAM}>"
    assert second == "brotli <1.2.1 · published 22 Oct 2026"


def test_a_package_github_says_has_no_fix_says_so(world, monkeypatch):
    rows = saved("grype-db-search-brotli.json")
    for affected in rows[0]["packages"]:
        affected["detail"]["ranges"][0]["fix"] = {"state": "not-fixed"}
    monkeypatch.setattr(tools, "db_search", lambda *ids: rows)
    assert searched().split("\n")[1] == "brotli >=1.0.3,<1.2.1 · no fixed version yet · published 22 Oct 2026"


def test_known_exploited_appears_only_if_the_record_says_so(world, monkeypatch):
    rows = saved("grype-db-search-brotli.json")
    for match in rows:
        match["vulnerability"].pop("known_exploited")
    monkeypatch.setattr(tools, "db_search", lambda *ids: rows)
    assert "known exploited" not in searched()


def test_the_time_is_never_rounded_down(world, monkeypatch):
    import run_state

    monkeypatch.setattr(run_state, "now", lambda: REQUESTED + 30.2)
    assert "live services in 31s:" in searched()
    monkeypatch.setattr(run_state, "now", lambda: REQUESTED + 75)
    assert "live services in 1m 15s:" in searched()


# What can't be trusted is never clear.

def only_row(text, service):
    return next(line for line in text.splitlines() if service in line)


def test_a_failed_read_says_which_error_and_isnt_clear(world):
    world.functions.configs["pricing-api-production"] = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetAlias")
    text = searched()
    assert only_row(text, "pricing-api") == ":warning:  pricing-api: couldn't read what's live (AccessDeniedException)"
    assert "1 of 3 holds it. 1 couldn't be searched." in text


def test_the_role_denied_at_the_door_makes_every_row_say_so(world, monkeypatch):
    def denied(session=None):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "AssumeRole")

    monkeypatch.setattr(readers, "reader_client", denied)
    text = searched()
    for service in ("orders-service", "pricing-api", "quote-service"):
        assert only_row(text, f"  {service}") == f":warning:  {service}: couldn't read what's live (AccessDenied)"
    assert "clear" not in text and "0 of 3 hold it. 3 couldn't be searched." in text and world.scans == []


def test_no_sbom_stored_says_so(world):
    del world.store.files["demos-releases/pricing-api/2.1.2/sbom.cdx.json"]
    text = searched()
    assert only_row(text, "pricing-api") == ":warning:  pricing-api 2.1.2: no SBOM stored with this package"
    assert "1 couldn't be searched." in text


def test_no_release_folder_at_all_says_none_is_stored(world):
    for name in [name for name in world.store.files if name.startswith("demos-releases/pricing-api/")]:
        del world.store.files[name]
    assert only_row(searched(), "pricing-api") == ":warning:  pricing-api 2.1.2: no SBOM stored with this package"


def test_a_package_that_isnt_the_stored_one_doesnt_match(world):
    world.functions.configs["pricing-api-production"]["CodeSha256"] = b64digest(b"something else is live")
    text = searched()
    assert only_row(text, "pricing-api") == ":warning:  pricing-api 2.1.2: the stored SBOM doesn't match the live package"
    assert "clear" not in only_row(text, "pricing-api")


def test_an_image_whose_digest_differs_doesnt_match(world):
    world.functions.configs["quote-service-production"]["CodeSha256"] = "cd" * 32
    assert only_row(searched(), "quote-service") == ":warning:  quote-service 1.2.0: the stored SBOM doesn't match the live package"


def test_the_stored_sbom_swapped_for_another_versions_doesnt_match(world):
    world.store.files["orders-service-releases/1.12.0/sbom.cdx.json"] = saved_bytes("sbom-brotli-1.2.1.cdx.json")
    text = searched()
    assert only_row(text, "orders-service") == ":warning:  orders-service 1.12.0: the stored SBOM doesn't match the live package"
    assert "0 of 3 hold it. 1 couldn't be searched." in text
    assert len(world.scans) == 2  # the swapped one never reached Grype


def test_the_whole_folder_of_another_build_doesnt_match(world):
    """Build-info, SBOM and tie all from another package: the checksum of what is live still decides."""
    other = b"another build of 1.12.0"
    for name in list(world.store.files):
        if name.startswith("orders-service-releases/1.12.0/"):
            del world.store.files[name]
    released(world.store, "orders-service-releases", "1.12.0", other, saved_bytes("sbom-empty.cdx.json"))
    assert only_row(searched(), "orders-service") == ":warning:  orders-service 1.12.0: the stored SBOM doesn't match the live package"


def test_an_sbom_with_nothing_tying_it_to_the_package_doesnt_match(world):
    del world.store.files["orders-service-releases/1.12.0/sbom-info.json"]
    assert "doesn't match" in only_row(searched(), "orders-service")


def test_the_second_build_of_a_version_is_found_beside_the_first(world):
    live_package = b"build 9 of 1.12.0"
    released(world.store, "orders-service-releases/1.12.0", "9", live_package, saved_bytes("sbom-brotli-1.2.0.cdx.json"))
    world.functions.configs["orders-service-production"]["CodeSha256"] = b64digest(live_package)
    assert ":x:  orders-service 1.12.0 holds brotli 1.2.0" in searched()


def test_a_scan_that_fails_isnt_clear(world, monkeypatch):
    def fails(sbom_file):
        raise tools.GrypeFailed("grype exited 1")

    monkeypatch.setattr(tools, "scan", fails)
    text = searched()
    assert only_row(text, "pricing-api") == ":warning:  pricing-api 2.1.2: Grype couldn't read its SBOM (GrypeFailed)"
    assert "clear" not in text and "3 couldn't be searched." in text


def test_a_report_without_matches_isnt_clear(world, monkeypatch):
    monkeypatch.setattr(tools, "scan", lambda sbom_file: {"descriptor": {"name": "grype"}})
    assert "clear" not in searched()


def test_jfrog_refusing_isnt_clear(world, monkeypatch):
    class Refusing:
        def get(self, path):
            return 403, b"forbidden"

    monkeypatch.setattr(readers, "evidence_reader", Refusing)
    text = searched()
    assert only_row(text, "pricing-api") == ":warning:  pricing-api 2.1.2: couldn't read JFrog (403)"
    assert "clear" not in text


def test_jfrogs_answers_reach_the_reader_as_statuses(monkeypatch):
    class Answer(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout):
        assert request.get_method() == "GET" and request.full_url == "http://jfrog.test/artifactory/a/b.json"
        assert request.get_header("Authorization") == "Bearer the-token"
        if urlopen.missing:
            raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b"gone"))
        return Answer(b"body")

    monkeypatch.setattr(readers.urllib.request, "urlopen", urlopen)
    jfrog = readers.JFrog("the-token", base="http://jfrog.test/")
    urlopen.missing = False
    assert jfrog.get("a/b.json") == (200, b"body")
    urlopen.missing = True
    assert jfrog.get("a/b.json") == (404, b"gone") and readers.present(jfrog, "a/b.json") is None


def test_a_missing_jfrog_token_isnt_clear(world, monkeypatch):
    def no_token():
        raise SystemExit(1)

    monkeypatch.setattr(readers, "evidence_reader", no_token)
    assert "couldn't read JFrog (NoJFrogToken)" in only_row(searched(), "pricing-api")


def test_a_running_canary_isnt_read_as_one_version(world):
    world.functions.configs["pricing-api-production"]["RoutingConfig"] = {"AdditionalVersionWeights": {"8": 0.1}}
    assert only_row(searched(), "pricing-api") == ":warning:  pricing-api: couldn't read what's live (CanaryRunning)"


def test_a_version_terraform_published_has_no_app_version_to_read(world):
    world.functions.configs["pricing-api-production"]["Description"] = "pricing-api in production."
    assert only_row(searched(), "pricing-api") == ":warning:  pricing-api: couldn't read what's live (NoAppVersion)"


def test_every_clear_row_comes_after_a_real_scan(world):
    text = searched()
    assert text.count(":white_check_mark:") == 2 and len(world.scans) == 3


def test_several_packages_held_stay_short(world, monkeypatch):
    report = saved("grype-by-cve-brotli-1.2.0.json")
    report["matches"].append(json.loads(json.dumps(report["matches"][0])))
    report["matches"][1]["artifact"]["name"] = "brotlicffi"
    monkeypatch.setattr(tools, "scan", lambda sbom_file: report)
    assert ":x:  orders-service 1.12.0 holds brotli 1.2.0 and 1 more" in searched()


def test_the_advisorys_other_package_shows_when_it_is_the_one_held(world, monkeypatch):
    report = saved("grype-by-cve-brotli-1.2.0.json")
    report["matches"][0]["artifact"].update(name="brotlicffi", version="1.2.0.0")
    monkeypatch.setattr(tools, "scan", lambda sbom_file: report)
    assert "\nbrotlicffi >=1.0.3,<1.2.1.0 · fixed in 1.2.1.0 · published 22 Oct 2026\n" in searched()


# The id.

@pytest.mark.parametrize("text, expected", [("CVE-2026-111497", "CVE-2026-111497"), ("cve-2026-111497", "CVE-2026-111497"),
                                            ("GHSA-9v8h-qqgm-4m35", "GHSA-9v8h-qqgm-4m35"), ("ghsa-9V8H-qqgm-4m35", "GHSA-9v8h-qqgm-4m35"),
                                            ("rm -rf /", None), ("CVE-2026-111497 x", None), ("--vuln", None), ("", None), (None, None),
                                            ("../../etc/passwd", None)])
def test_only_a_cve_or_ghsa_id_passes(text, expected):
    assert search.vulnerability_id(text) == expected


def test_a_bad_id_in_the_jobs_parameter_searches_nothing(world, monkeypatch, capsys):
    monkeypatch.setenv("VULN", "$(whoami)")
    assert search.main() == 1
    assert capsys.readouterr().out.strip() == "Usage: /sbom <CVE or GHSA id>, for example /sbom CVE-2026-111497"
    assert world.scans == []


def test_a_search_that_fails_rewrites_the_message_with_the_errors_name(world, monkeypatch):
    sent = []
    monkeypatch.setenv("VULN", "CVE-2026-111497")
    monkeypatch.setenv("ASKED_BY", SAM)
    monkeypatch.setenv("SLACK_CHANNEL", "C0RELEASES")
    monkeypatch.setenv("SLACK_TS", "1793.0001")
    monkeypatch.setattr(search, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(search, "ssm_client", lambda: None)
    monkeypatch.setattr(search.notify, "slack", lambda method, token, **fields: sent.append((method, fields)))

    def down():
        raise tools.GrypeFailed("Grype's database isn't usable after an update: secret-looking detail")

    monkeypatch.setattr(tools, "usable_database", down)
    assert search.main() == 1
    assert sent == [("chat.update", {"channel": "C0RELEASES", "ts": "1793.0001", "text":
                     f"*Search for CVE-2026-111497* · asked by <@{SAM}>\n\n:x:  The search failed (GrypeFailed). The job's log says why."})]


def test_the_answer_rewrites_the_handlers_message(world, monkeypatch):
    sent = []
    monkeypatch.setenv("VULN", "CVE-2026-111497")
    monkeypatch.setenv("ASKED_BY", SAM)
    monkeypatch.setenv("REQUESTED_AT", str(int(REQUESTED)))
    monkeypatch.setenv("SLACK_CHANNEL", "C0RELEASES")
    monkeypatch.setenv("SLACK_TS", "1793.0001")
    monkeypatch.setattr(search, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(search, "ssm_client", lambda: None)
    monkeypatch.setattr(search.notify, "slack", lambda method, token, **fields: sent.append((method, token, fields)))
    assert search.main() == 0
    (method, token, fields), = sent
    assert method == "chat.update" and token == "token" and fields["ts"] == "1793.0001"
    assert "in 31s:" in fields["text"] and "1 of 3 holds it." in fields["text"]


# Who it reads, and what it may touch.

def test_three_services_are_searched_and_the_rest_listed():
    assert set(readers.searchable()) == {"orders-service", "pricing-api", "quote-service"}
    assert {block["reader"] for block in readers.searchable().values()} == {"lambda-alias"}
    assert {block["function"] for block in readers.searchable().values()} == {"orders-service-production", "pricing-api-production", "quote-service-production"}
    others = readers.not_searched()
    assert {"catalog", "inventory", "runbook-assistant", "store-assistant", "store-floor", "store-sync"} <= set(others)
    assert "network-stack" not in others and not set(others) & set(readers.searchable())


def test_a_new_reader_is_one_function():
    assert readers.READERS["lambda-alias"] is readers.lambda_alias


def test_an_images_codesha256_is_its_digest_and_a_zips_is_base64():
    functions = {"f": {"Description": "1.2.0", "CodeSha256": "ab" * 32, "PackageType": "Image"}}
    client = type("C", (), {"get_alias": lambda self, **k: {"FunctionVersion": "7"}, "get_function_configuration": lambda self, **k: functions["f"]})()
    what = readers.lambda_alias(client, "f")
    assert what.digest == f"sha256:{'ab' * 32}" and not what.sha256
    assert what.is_named_by({"image": {"digest": what.digest}}) and what.is_named_by({"image_digest": what.digest})
    assert not what.is_named_by({"sha256": "ab" * 32})
    functions["f"] = {"Description": "1.2.0", "CodeSha256": b64digest(b"zip"), "PackageType": "Zip"}
    assert readers.lambda_alias(client, "f").is_named_by({"sha256": hexdigest(b"zip")})


WRITES = {"change", "deploy", "stage"}
READ_FROM_PUBLISH = {"publish.JCR", "publish.SBOM", "publish.SBOM_INFO", "publish.releases_root"}


def imports_of(path):
    """Plain imports by name, and from-imports as module.name, at any depth of the file."""
    names = set()
    for node in ast.walk(ast.parse(Path(path).read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("module", ["search.py", "live.py", "tools.py"])
def test_the_search_imports_nothing_that_writes(module):
    names = imports_of(HERE / module)
    assert not {name.split(".")[0] for name in names} & WRITES
    assert "publish" not in names and {name for name in names if name.startswith("publish.")} <= READ_FROM_PUBLISH


def test_importing_the_search_loads_nothing_that_writes():
    code = "import sys; sys.path.insert(0, %r); import search; print(sorted(set(sys.modules) & {'change', 'deploy', 'stage', 'servicenow_setup', 'jira', 'tag'}))" % str(HERE)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_a_search_makes_only_the_two_lambda_reads(world):
    searched()
    assert {name for name, _ in world.functions.calls} == {"GetAlias", "GetFunctionConfiguration"}
    assert world.store.puts == []


# The tools.

def test_the_pins_are_toolchain_groovys():
    pinned = dict(re.findall(r"@Field String (SONAR|SYFT|GRYPE) = '([^']+)'", tools.TOOLCHAIN.read_text()))
    assert tools.pins() == {"SYFT": pinned["SYFT"], "GRYPE": pinned["GRYPE"]}
    assert tools.pins()["GRYPE"].startswith("anchore/grype:v0.120.1@sha256:")
    assert tools.pins()["SYFT"].startswith("anchore/syft:v1.54.1@sha256:")


@pytest.fixture
def docker(tmp_path, monkeypatch):
    calls = []

    def run(command, capture_output, text, timeout):
        calls.append(command)
        for mount in command:
            if mount.endswith(":/work"):  # Syft writes its SBOM here
                (Path(mount.removesuffix(":/work")) / "sbom.cdx.json").write_bytes(b"{}")
        return subprocess.CompletedProcess(command, 0, stdout='{"matches": [], "valid": true}', stderr="")

    monkeypatch.setattr(tools, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(tools.subprocess, "run", run)
    return calls


def test_a_scan_runs_the_pinned_image_with_auto_update_off_on_the_shared_cache(docker, tmp_path):
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}")
    assert tools.scan(sbom) == {"matches": [], "valid": True}
    command = docker[0]
    assert command[:2] == ["docker", "run"] and tools.pins()["GRYPE"] in command
    assert f"{tmp_path}/cache/grype:/cache" in command and f"{tmp_path}:/work:ro" in command
    assert "GRYPE_DB_AUTO_UPDATE=false" in command and "GRYPE_DB_CACHE_DIR=/cache" in command and "TMPDIR=/cache" in command
    assert command[-4:] == ["sbom:/work/sbom.cdx.json", "--by-cve", "-o", "json"]


def test_syft_runs_as_the_stage_runs_it(docker, tmp_path):
    work = tmp_path / "work"
    (work / "package").mkdir(parents=True)
    assert tools.syft(work / "package", work, "orders-service") == b"{}"
    command = docker[0]
    assert tools.pins()["SYFT"] in command and "SYFT_CACHE_DIR=/cache" in command and "TMPDIR=/cache" in command
    assert command[-5:] == ["dir:/work/package", "--source-name", "orders-service", "-o", "cyclonedx-json=/work/sbom.cdx.json"]
    (work / "package" / "image").mkdir()
    (work / "package" / "image" / "index.json").write_text("{}")
    tools.syft(work / "package", work, "quote-service")
    assert "oci-dir:/work/package/image" in docker[1]
    stage = (ROOT / "jenkins" / "shared-library" / "vars" / "toolchain.groovy").read_text()
    assert "source=dir:/work/.release/package" in stage and "source=oci-dir:/work/.release/package/image" in stage
    assert "--source-name ${service} -o cyclonedx-json=" in stage and "-e SYFT_CACHE_DIR=/cache -e TMPDIR=/cache" in stage


def test_a_warm_database_isnt_downloaded_and_a_stale_one_is_updated_once(monkeypatch):
    calls = []
    states = [{"valid": False, "error": "built 6 days ago"}, {"valid": True, "built": "2026-11-02T00:00:00Z"}]
    monkeypatch.setattr(tools, "db_status", lambda: states.pop(0))
    monkeypatch.setattr(tools, "db_update", lambda: calls.append("update"))
    assert tools.usable_database()["valid"] and calls == ["update"]
    calls.clear()
    monkeypatch.setattr(tools, "db_status", lambda: {"valid": True})
    assert tools.usable_database()["valid"] and calls == []


def test_a_database_that_stays_unusable_is_an_error(monkeypatch):
    monkeypatch.setattr(tools, "db_status", lambda: {"valid": False, "error": "no network"})
    monkeypatch.setattr(tools, "db_update", lambda: "")
    with pytest.raises(tools.GrypeFailed):
        tools.usable_database()


def test_the_alias_table_gives_the_ghsa_of_a_cve(tmp_path, monkeypatch):
    """The real table's columns, from Grype v0.120.1's models.go: name, alias."""
    monkeypatch.setattr(tools, "CACHE", tmp_path / "cache")
    folder = tmp_path / "cache" / "grype" / "6"
    folder.mkdir(parents=True)
    connection = sqlite3.connect(folder / "vulnerability.db")
    connection.execute("CREATE TABLE vulnerability_aliases (name TEXT, alias TEXT)")
    connection.executemany("INSERT INTO vulnerability_aliases VALUES (?, ?)", [("GHSA-9v8h-qqgm-4m35", "CVE-2026-111497"), ("RHSA-2026:1", "CVE-2026-111497"),
                                                                                ("GHSA-aaaa-bbbb-cccc", "CVE-2026-2")])
    connection.commit()
    connection.close()
    status = {"path": "/cache/6/vulnerability.db"}
    assert search.ghsa_ids("CVE-2026-111497", status) == ["GHSA-9v8h-qqgm-4m35"]
    assert search.ghsa_ids("cve-2026-111497", status) == ["GHSA-9v8h-qqgm-4m35"]
    assert search.ghsa_ids("CVE-2020-1", status) == []


def test_an_unreadable_alias_table_only_means_fewer_facts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tools, "CACHE", tmp_path / "cache")
    assert search.ghsa_ids("CVE-2026-111497", {"path": "/cache/6/vulnerability.db"}) == []
    assert "not read" in capsys.readouterr().out


def test_read_facts_asks_by_the_cve_and_its_ghsa(world, monkeypatch):
    asked = []
    monkeypatch.setattr(tools, "db_search", lambda *ids: asked.append(ids) or saved("grype-db-search-brotli.json"))
    facts = search.read_facts("CVE-2026-111497")
    assert asked == [("CVE-2026-111497", "GHSA-9v8h-qqgm-4m35")]
    assert facts["names"] == {"brotli", "brotlicffi"} and facts["published"] == "22 Oct 2026" and facts["severity"] == "Critical"
    assert {"CVE-2026-111497", "GHSA-9V8H-QQGM-4M35"} <= facts["ids"]


def test_at_most_three_scans_run_at_once(world, monkeypatch):
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        released(world.store, f"demos-releases/{name}", "1.0.0", name.encode(), saved_bytes("sbom-empty.cdx.json"))
        world.functions.configs[f"{name}-production"] = {"Version": "1", "Description": "1.0.0", "CodeSha256": b64digest(name.encode()), "PackageType": "Zip"}
    monkeypatch.setattr(search.readers, "releases_root", lambda name: f"demos-releases/{name}", raising=True)
    running, most, lock, three_at_once = [0], [0], threading.Lock(), threading.Event()

    def scan(sbom_file):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
            if running[0] == 3:
                three_at_once.set()
        three_at_once.wait(10)  # the first three wait for each other; if a fourth could start, most would show it
        with lock:
            running[0] -= 1
        return saved("grype-by-cve-clear.json")

    monkeypatch.setattr(tools, "scan", scan)
    services = {name: {"reader": "lambda-alias", "function": f"{name}-production"} for name in ("alpha", "beta", "gamma", "delta", "epsilon")}
    facts = search.read_facts("CVE-2026-111497")
    rows = search.rows_for(services, world.functions, world.store, facts)
    assert [row.service for row in rows] == list(services) and most[0] == 3


# With and without the switch.

def test_with_the_switch_on_a_holder_opens_the_clock(world):
    searched()
    (opened,) = clock.open_clocks()
    assert opened["id"] == "CVE-2026-111497" and opened["requested_at"] == REQUESTED
    assert opened["holders"] == {"orders-service": {"package": "brotli", "version": "1.2.0"}}


def test_with_the_switch_off_the_search_still_answers_and_nothing_else_happens(world, monkeypatch):
    monkeypatch.setattr(search.SWITCH, "is_on", lambda ssm=None: False)
    assert "1 of 3 holds it." in searched()
    assert clock.open_clocks() == []


def test_no_holder_opens_no_clock(world):
    world.patched()
    searched()
    assert clock.open_clocks() == []


def test_a_clock_that_cant_be_written_doesnt_stop_the_answer(world, monkeypatch):
    def refused(*args, **kwargs):
        raise PermissionError("no")

    monkeypatch.setattr(clock, "start", refused)
    assert "1 of 3 holds it." in searched()


def test_the_jobs_are_defined_as_the_spec_names_them():
    jobs = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    search_job = jobs[jobs.index("pipelineJob('sbom-search')"):jobs.index("pipelineJob('patch-clock-seed')")]
    assert re.findall(r"stringParam\('(\w+)'", search_job) == ["VULN", "ASKED_BY", "REQUESTED_AT", "SLACK_CHANNEL", "SLACK_TS"]
    assert "disableConcurrentBuilds" not in search_job and "fromRepository('jenkins/sbom-search.Jenkinsfile')" in search_job
    jenkinsfile = (ROOT / "jenkins" / "sbom-search.Jenkinsfile").read_text()
    assert "agent { label 'tools-host' }" in jenkinsfile and "timeout(time: 5, unit: 'MINUTES')" in jenkinsfile
    assert "sh 'python3 scripts/demos/patch_clock/search.py'" in jenkinsfile
