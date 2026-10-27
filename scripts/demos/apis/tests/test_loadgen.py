import io
import json

import loadgen


class Invoking:
    exceptions = type("E", (), {"TooManyRequestsException": type("Throttled", (Exception,), {})})

    def __init__(self, error=False):
        self.error = error

    def invoke(self, FunctionName, Qualifier, Payload):
        event = json.loads(Payload)
        assert Qualifier == "live" and event["queryStringParameters"]["store"] == "BE-ANT-01"
        if self.error:
            return {"ExecutedVersion": "2", "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(b'{"errorType": "KeyError", "errorMessage": "\'HL-320\'"}')}
        body = {"statusCode": 200, "headers": {"x-pricing-api-version": "2.0.4"}, "body": "{}"}
        return {"ExecutedVersion": "1", "Payload": io.BytesIO(json.dumps(body).encode())}


def test_a_call_reports_status_and_version():
    status, number, app, error, took = loadgen.call(Invoking(), "f", "/prices/HL-320", "BE-ANT-01")
    assert (status, number, app, error) == (200, "1", "2.0.4", "")


def test_a_raised_error_is_a_500_from_its_own_version():
    status, number, app, error, took = loadgen.call(Invoking(error=True), "f", "/prices/HL-320", "BE-ANT-01")
    assert (status, number, app, error) == (500, "2", None, "KeyError")


def test_totals_per_version():
    totals = loadgen.Totals()
    for failed in (0, 0, 0, 1):
        totals.add({"version": "2.1.1", "failed": failed, "latency_ms": 40.0})
    assert list(totals.lines()) == ["2.1.1: 4 requests, 1 failed (25.0%), p95 40 ms"]


def test_the_event_is_the_one_api_gateway_sends():
    event = loadgen.api_event("/prices/RS-100", "NL-UTR-01")
    assert event["version"] == "2.0" and event["rawPath"] == "/prices/RS-100"
    assert event["requestContext"]["http"]["method"] == "GET"
