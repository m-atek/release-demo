"""Video 12's note: the request the real SDK sends, the note's fixed form, and
the notes that say why there is no summary."""

import copy

import llm
import summarize
from conftest import Recorder, no_findings, sdk_response
from test_verify import REAL_SUMMARY

NOTE = """Summary for the change manager
Written by claude-opus-5-5 from this run's evidence. Every number checked against the attachments by script.

What changes
Version 1.4.1 reads the delivery estimate from the regional setting DELIVERY_ESTIMATE_DAYS in handler.py, where 1.4.0 showed a fixed 2 days.

Results
- Tests: All 10 tests passed, with 0 failed and 0 errors. [junit.xml]
- Quality gate: The SonarQube quality gate passed. [SonarQube]
- Scan: The scan found 0 critical and 0 high vulnerabilities in 3 SBOM components. [grype.json]
- Package: The package is orders-service-1.4.1.zip, recorded with its SHA-256. [build-info.json]

Not checked by the pipeline
- 1.4.1 reads DELIVERY_ESTIMATE_DAYS, which production doesn't define.
  Found by evidence.py: handler.py line 84 reads it with no default. Defined in staging, not in production."""


def test_the_note_in_its_fixed_form(take_evidence):
    recorder = Recorder(sdk_response(REAL_SUMMARY))
    text, answer = summarize.summarize(take_evidence, "orders-service", client=recorder.client(), scan=no_findings)
    assert text == NOTE
    assert answer.model == "claude-opus-5-5"


def test_the_request_as_the_sdk_sends_it(take_evidence):
    recorder = Recorder(sdk_response(REAL_SUMMARY))
    summarize.summarize(take_evidence, "orders-service", client=recorder.client(), scan=no_findings)
    (request,) = recorder.requests
    body, headers = request["body"], request["headers"]
    assert request["url"] == "https://api.anthropic.com/v1/messages"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000 and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": summarize.SCHEMA}
    assert body["system"] == [{"type": "text", "text": summarize.SYSTEM, "cache_control": {"type": "ephemeral"}}]
    assert "temperature" not in body and "thinking" not in body
    assert '"DELIVERY_ESTIMATE_DAYS"' in body["messages"][0]["content"]


def test_every_object_in_the_schema_is_closed_and_complete():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert sorted(node["required"]) == sorted(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(summarize.SCHEMA)


def test_one_unmatched_statement_withholds_the_whole_summary(take_evidence):
    wrong = copy.deepcopy(REAL_SUMMARY)
    wrong["results"]["tests"]["sentence"] = "All 12 tests passed."
    text, _ = summarize.summarize(take_evidence, "orders-service",
                                  client=Recorder(sdk_response(wrong)).client(), scan=no_findings)
    assert text == ("No summary for this change. 1 statement didn't match the evidence: Tests: 12 isn't in the "
                    "evidence. The evidence is attached as usual.")


def test_an_api_error_a_refusal_and_no_gitleaks_each_say_why(take_evidence):
    refused = sdk_response({}, stop_reason="refusal", content=[])
    refused["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": None}
    text, answer = summarize.summarize(take_evidence, "orders-service", client=Recorder(refused).client(),
                                       scan=no_findings)
    assert text == "No summary for this change: Claude declined the request (cyber). The evidence is attached as usual."
    assert answer is None
    down = Recorder(status=529)
    text, _ = summarize.summarize(take_evidence, "orders-service", client=down.client(), scan=no_findings)
    assert text == "No summary for this change: the API answered 529. The evidence is attached as usual."

    def broken(text):
        raise llm.LLMError("gitleaks can't run here (no Docker, no gitleaks 8.30.1), so nothing was sent")
    recorder = Recorder(sdk_response(REAL_SUMMARY))
    text, _ = summarize.summarize(take_evidence, "orders-service", client=recorder.client(), scan=broken)
    assert "gitleaks can't run here" in text and recorder.requests == []


def test_no_finding_reads_as_the_script_found_nothing():
    from conftest import release_evidence

    evidence = release_evidence("1.3.0", "1.4.0")
    data = copy.deepcopy(REAL_SUMMARY)
    data["what_changes"] = {"sentence": "Version 1.4.0 adds the line Delivery estimate: 2 days to the page.",
                            "names": ["1.4.0"], "numbers": []}
    data["results"]["package"]["sentence"] = "The package is orders-service-1.4.0.zip."
    data["not_checked"] = []
    text, _ = summarize.summarize(evidence, "orders-service", client=Recorder(sdk_response(data)).client(),
                                  scan=no_findings)
    assert text.endswith("[build-info.json]\n\nNot checked by the pipeline: evidence.py found nothing.")


def test_a_finding_defined_nowhere(take_evidence):
    finding = {"id": "F1", "setting": "STORES", "file": "handler.py", "line": 86, "defined_in": [],
               "missing_in": ["staging", "production"], "new": True}
    assert summarize.found_line(finding) == ("Found by evidence.py: handler.py line 86 reads it with no default. "
                                             "Defined in no environment, not in staging or production.")
