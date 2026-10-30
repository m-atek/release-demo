"""Video 12's acceptance check 4 and video 13's check 3: what the verifier lets
through, and what it withholds or drops, on a real evidence.json."""

import copy

import verify

REAL_SUMMARY = {
    "what_changes": {
        "sentence": "Version 1.4.1 reads the delivery estimate from the regional setting DELIVERY_ESTIMATE_DAYS "
                    "in handler.py, where 1.4.0 showed a fixed 2 days.",
        "names": ["1.4.1", "1.4.0", "DELIVERY_ESTIMATE_DAYS", "handler.py"], "numbers": []},
    "results": {
        "tests": {"sentence": "All 10 tests passed, with 0 failed and 0 errors.",
                  "numbers": [{"value": 10, "key": "results.tests.passed"}, {"value": 0, "key": "results.tests.failed"},
                              {"value": 0, "key": "results.tests.errors"}]},
        "quality_gate": {"sentence": "The SonarQube quality gate passed.", "numbers": []},
        "scan": {"sentence": "The scan found 0 critical and 0 high vulnerabilities in 3 SBOM components.",
                 "numbers": [{"value": 0, "key": "results.scan.critical"}, {"value": 0, "key": "results.scan.high"},
                             {"value": 3, "key": "results.sbom.components"}]},
        "package": {"sentence": "The package is orders-service-1.4.1.zip, recorded with its SHA-256.", "numbers": []},
    },
    "not_checked": [{"finding": "F1", "sentence": "1.4.1 reads DELIVERY_ESTIMATE_DAYS, which production doesn't define.",
                     "numbers": []}],
}


def changed(**edits):
    summary = copy.deepcopy(REAL_SUMMARY)
    for path, value in edits.items():
        target = summary
        *parents, last = path.split("__")
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[last] = value
    return summary


def test_the_real_summary_passes(take_evidence):
    assert verify.summary_failures(REAL_SUMMARY, take_evidence) == []


def test_a_changed_number_withholds_it(take_evidence):
    failures = verify.summary_failures(changed(results__tests__sentence="All 11 tests passed, with 0 failed and 0 errors."),
                                       take_evidence)
    assert failures == ["Tests: 11 isn't in the evidence"]
    listed = changed(results__tests__numbers=[{"value": 11, "key": "results.tests.passed"}],
                     results__tests__sentence="All 11 tests passed.")
    assert "Tests: results.tests.passed is 10, not 11" in verify.summary_failures(listed, take_evidence)


def test_a_number_from_the_diff_is_fine_but_not_in_a_result(take_evidence):
    # "2 days" is in the line 1.4.1 removed; a result has to name its key.
    assert verify.summary_failures(REAL_SUMMARY, take_evidence) == []
    failures = verify.summary_failures(changed(results__quality_gate__sentence="The gate passed in 2 checks."),
                                       take_evidence)
    assert failures == ["Quality gate: 2 isn't in the evidence"]


def test_an_unknown_setting_name_withholds_it(take_evidence):
    failures = verify.summary_failures(changed(
        what_changes__sentence="Version 1.4.1 reads DELIVERY_WINDOW_DAYS in handler.py."), take_evidence)
    assert failures == ["What changes: unknown setting DELIVERY_WINDOW_DAYS"]


def test_an_unknown_version_withholds_it(take_evidence):
    failures = verify.summary_failures(changed(
        what_changes__sentence="Version 1.4.2 reads DELIVERY_ESTIMATE_DAYS in handler.py."), take_evidence)
    assert failures == ["What changes: unknown version 1.4.2"]


def test_a_finding_that_doesnt_exist_withholds_it(take_evidence):
    failures = verify.summary_failures(changed(not_checked__0__finding="F2"), take_evidence)
    assert failures == ["Not checked F2: no such finding", "Not checked: no item for F1"]


def test_an_item_that_doesnt_name_the_setting_or_environment(take_evidence):
    failures = verify.summary_failures(changed(not_checked__0__sentence="1.4.1 reads a setting staging has."),
                                       take_evidence)
    assert failures == ["Not checked F1: doesn't name DELIVERY_ESTIMATE_DAYS", "Not checked F1: doesn't name production"]


def test_a_missing_results_line_withholds_it(take_evidence):
    assert verify.summary_failures(changed(results__scan__sentence=""), take_evidence) == ["Scan: missing"]
    assert verify.summary_failures(changed(results__package__sentence="  "), take_evidence) == ["Package: missing"]


def test_identifiers_with_digits_are_not_numbers():
    assert verify.numbers_in("ORD-13 in CHG0030071, SHA-256 9f2c1ab, version 1.4.1 on x86") == []
    assert verify.numbers_in("10 tests, 1,024 components and 3.5 seconds.") == [10.0, 1024.0, 3.5]


CHECK = ["FAIL /health reports 1.4.0, expected 1.4.1", "ok   /health reports 1.4.1 after 3s", "FAIL / answered 502",
         "ok   /api/orders answered 200", "Production check failed"]
LOGS = ["LAMBDA_WARNING: Unhandled exception. The most likely cause is an issue in the function code.",
        "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'", "Traceback (most recent call last):",
        '  File "/var/task/handler.py", line 84, in delivery_estimate_days',
        '    return int(os.environ["DELIVERY_ESTIMATE_DAYS"])']
QUOTES = [{"source": "cloudwatch", "quote": "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'"},
          {"source": "diff", "quote": '+    return int(os.environ["DELIVERY_ESTIMATE_DAYS"])'},
          {"source": "settings", "quote": "production: DELIVERY_DAYS, ENVIRONMENT"}]


def sources(evidence):
    return {"cloudwatch": LOGS, "check": CHECK, "diff": evidence["diff"]["text"].splitlines(),
            "settings": [f"{env}: {', '.join(names)}" for env, names in evidence["settings"]["defined"].items()]}


def test_every_real_quote_is_found_word_for_word(take_evidence):
    kept, dropped = verify.check_quotes({"evidence": QUOTES}, sources(take_evidence))
    assert [item["quote"] for item in kept] == ["[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'",
                                                'return int(os.environ["DELIVERY_ESTIMATE_DAYS"])',
                                                "production: DELIVERY_DAYS, ENVIRONMENT"]
    assert dropped == []


def test_a_quote_changed_by_one_character_is_dropped(take_evidence):
    quotes = [{"source": "cloudwatch", "quote": "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAY'"}, *QUOTES[1:]]
    kept, dropped = verify.check_quotes({"evidence": quotes}, sources(take_evidence))
    assert len(kept) == 2 and dropped == [{"source": "cloudwatch", "quote": "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAY'"}]


def test_a_quote_from_the_wrong_source_or_part_of_a_line_is_dropped(take_evidence):
    quotes = [{"source": "check", "quote": "[ERROR] KeyError: 'DELIVERY_ESTIMATE_DAYS'"},
              {"source": "cloudwatch", "quote": "KeyError: 'DELIVERY_ESTIMATE_DAYS'"},
              {"source": "diff", "quote": "+++ b/handler.py"}, {"source": "check", "quote": "FAIL / answered 502"}]
    kept, dropped = verify.check_quotes({"evidence": quotes}, sources(take_evidence))
    assert [item["source"] for item in dropped] == ["check", "cloudwatch", "diff"] and kept == []


def test_names_outside_the_sources_are_reported():
    known = verify.known_names({"lines": LOGS + CHECK})
    assert verify.unknown_names("1.4.1 reads DELIVERY_ESTIMATE_DAYS in handler.py", known) == []
    assert verify.unknown_names("It reads REGION_DAYS from config.yaml in 2.0.0", known) == [
        "setting REGION_DAYS", "file config.yaml", "version 2.0.0"]
