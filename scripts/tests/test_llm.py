"""scripts/llm.py: the request every Claude call sends, what it refuses to
send, how it reads an answer, and what a call costs. The real SDK, with a
mock transport: nothing leaves the machine."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demos" / "ai_notes" / "tests"))

import llm  # noqa: E402
from conftest import Recorder, no_findings, sdk_response  # noqa: E402

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
          "properties": {"answer": {"type": "string"}}}


def test_setting_names_go_out_but_never_a_value_map():
    assert not llm.has_value_map({"defined": {"production": ["DELIVERY_DAYS", "ENVIRONMENT"]}})
    assert not llm.has_value_map({"read": [{"name": "DELIVERY_ESTIMATE_DAYS", "line": 84, "required": True}]})
    assert llm.has_value_map({"settings": {"production": {"DELIVERY_DAYS": "2"}}})
    assert llm.has_value_map([{"ENVIRONMENT": "production"}])


def test_a_value_map_is_refused_before_anything_is_sent():
    recorder = Recorder(sdk_response({"answer": "x"}))
    with pytest.raises(llm.LLMError, match="setting values"):
        llm.ask("change_summary", "orders-service", "system", SCHEMA, {"DELIVERY_DAYS": "2"},
                client=recorder.client(), scan=no_findings)
    assert recorder.requests == []


def test_the_model_and_effort_come_from_services_yaml(monkeypatch):
    assert llm.task_settings("orders-service", "change_summary") == ("claude-opus-5-5", "medium")
    assert llm.task_settings("orders-service", "likely_cause") == ("claude-opus-5-5", "medium")
    with pytest.raises(llm.LLMError, match="no ai_notes"):
        llm.task_settings("pricing-api", "change_summary")


def test_fallbacks_only_where_the_model_has_them():
    opus = llm.request_body("claude-opus-5-5", "low", "s", SCHEMA, "t")
    assert (opus["betas"], opus["fallbacks"]) == (["server-side-fallback-2026-07-01"], "default")
    haiku = llm.request_body("claude-haiku-5-5", "low", "s", SCHEMA, "t")
    assert "fallbacks" not in haiku and "betas" not in haiku
    assert opus["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert opus["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}


def test_stop_reason_is_read_before_the_content():
    for stop, reason in (("max_tokens", "ran past 16000 tokens"), ("pause_turn", "stopped early (pause_turn)")):
        recorder = Recorder(sdk_response({"answer": "x"}, stop_reason=stop))
        with pytest.raises(llm.LLMError, match=re.escape(reason)):
            llm.ask("change_summary", "orders-service", "s", SCHEMA, {"a": 1}, client=recorder.client(),
                    scan=no_findings)
    prose = Recorder(sdk_response(None, content=[{"type": "text", "text": "Here is the summary."}]))
    with pytest.raises(llm.LLMError, match="wasn't the JSON"):
        llm.ask("change_summary", "orders-service", "s", SCHEMA, {"a": 1}, client=prose.client(), scan=no_findings)


def test_the_answer_names_the_model_that_answered_and_prices_every_attempt():
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
             "iterations": [
                 {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 0,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                 {"type": "fallback_message", "model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 500,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]}
    recorder = Recorder(sdk_response({"answer": "x"}, model="claude-sonnet-5-5", usage=usage))
    answer = llm.ask("change_summary", "orders-service", "s", SCHEMA, {"a": 1}, client=recorder.client(),
                     scan=no_findings)
    assert answer.model == "claude-sonnet-5-5" and answer.data == {"answer": "x"}
    assert answer.cost_usd == pytest.approx((1000 * 4 + 1000 * 2 + 500 * 10) / 1e6)
    event = llm.usage_event(answer, "change_summary", {"service": "orders-service", "version": "1.4.1"})
    assert event["fallback"] is True and event["model"] == "claude-sonnet-5-5" and "answer" not in json.dumps(event)


def test_a_summary_of_twelve_thousand_tokens_in_and_four_out_costs_about_thirteen_cents():
    usage = {"input_tokens": 12000, "output_tokens": 4000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert llm.cost("claude-opus-5-5", usage) == pytest.approx(0.128)
    assert llm.cost("claude-opus-5-5", {**usage, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 1000}) \
        == pytest.approx(0.128 + 0.0002 + 0.005)
    assert llm.price_of("claude-opus-5-5-20261001") == llm.price_of("claude-opus-5-5")
    with pytest.raises(llm.LLMError):
        llm.price_of("claude-unknown-1")


def test_the_prices_file_is_dated_and_names_its_pages():
    prices = llm.prices()
    assert str(prices["read"]) == "2026-10-30"
    assert prices["claude"]["source"].startswith("https://platform.claude.com/")
    assert prices["claude"]["per_million_tokens"]["claude-opus-5-5"] == {"input": 4.0, "output": 20.0,
                                                                         "cache_read": 0.2, "cache_write": 5.0}
    assert prices["aws"]["lambda"]["per_gb_second"] == 0.0000166667


def test_every_gitleaks_match_becomes_removed_and_is_counted():
    text, removed = llm.redact("a token tok-1 and tok-1 again, and pw-2", lambda t: [
        {"Secret": "tok-1", "Match": "token tok-1"}, {"Secret": "", "Match": "pw-2"}])
    assert text == "a token [removed] and [removed] again, and [removed]" and removed == 3


def test_nothing_is_sent_when_gitleaks_cant_run(monkeypatch):
    monkeypatch.setattr(llm, "gitleaks_command", lambda: None)
    recorder = Recorder(sdk_response({"answer": "x"}))
    with pytest.raises(llm.LLMError, match="nothing was sent"):
        llm.ask("change_summary", "orders-service", "s", SCHEMA, {"a": 1}, client=recorder.client())
    assert recorder.requests == []


def test_the_test_mode_logs_each_body_as_sent(tmp_path, monkeypatch):
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("AI_NOTES_REQUEST_LOG", str(log))
    recorder = Recorder(sdk_response({"answer": "x"}))
    llm.ask("change_summary", "orders-service", "s", SCHEMA, {"a": 1}, client=recorder.client(), scan=no_findings)
    logged = json.loads(log.read_text().splitlines()[0])
    assert logged["messages"] == recorder.requests[0]["body"]["messages"]


def test_an_unset_key_is_a_reason_not_a_crash():
    class Ssm:
        def get_parameter(self, Name, WithDecryption):
            return {"Parameter": {"Value": "unset"}}
    with pytest.raises(llm.LLMError, match="isn't set"):
        llm.api_key(Ssm())
