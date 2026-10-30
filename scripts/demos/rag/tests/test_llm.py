"""scripts/llm.py with a fake SDK: no call leaves the machine."""

import json
import shutil
from types import SimpleNamespace

import pytest

import llm

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}, "sources": {"type": "array"}},
          "required": ["answer", "sources"], "additionalProperties": False}
# The shape of an AWS access key id, made up, and put together here so the
# repository's own gitleaks scan doesn't take the test for a leak.
FAKE_KEY = "AKIA" + "QYLPMN5HHHFPZAM2"


class FakeMessages:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def create(self, **params):
        self.calls.append(params)
        return self.reply


def fake_sdk(text='{"answer": "Run /rollback orders-service.", "sources": ["RB-07"]}', stop="end_turn",
             model="claude-haiku-5-5"):
    reply = SimpleNamespace(stop_reason=stop, model=model, stop_details=None,
                            content=[SimpleNamespace(type="text", text=text)],
                            usage=SimpleNamespace(input_tokens=2000, output_tokens=100, cache_read_input_tokens=0,
                                                  cache_creation_input_tokens=0))
    messages = FakeMessages(reply)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)), messages


@pytest.fixture(autouse=True)
def no_redaction(monkeypatch):
    monkeypatch.setattr(llm, "redact", lambda text: (text, 0))


def test_a_caller_may_name_only_an_allowed_model():
    assert llm.choose("answer", "runbook-assistant", "claude-haiku-5-5", "low") == ("claude-haiku-5-5", "low")
    with pytest.raises(llm.Refused):
        llm.choose("answer", "runbook-assistant", "claude-opus-4-1", "low")
    with pytest.raises(llm.Refused):
        llm.choose("answer", "pricing-api")  # no ai_notes and no model named
    # orders-service's ai_notes (videos 12 and 13) name its model, when the caller names none.
    assert llm.choose("change_summary", "orders-service") == ("claude-opus-5-5", "medium")


def test_the_request_is_structured_cached_and_falls_back_where_it_can():
    haiku = llm.request("claude-haiku-5-5", "low", "system", "user", SCHEMA)
    assert haiku["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert haiku["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in haiku and "temperature" not in haiku
    sonnet = llm.request("claude-sonnet-5-5", "low", "system", "user", SCHEMA)
    assert sonnet["fallbacks"] == "default" and sonnet["betas"] == ["server-side-fallback-2026-07-01"]


def test_a_reply_becomes_data_with_its_model_tokens_and_cost(monkeypatch):
    monkeypatch.setattr(llm, "send_usage", lambda *a: None)
    sdk, messages = fake_sdk()
    result = llm.complete("answer", "system", "user", SCHEMA, "runbook-assistant", "claude-haiku-5-5", "low", sdk=sdk)
    assert result.data == {"answer": "Run /rollback orders-service.", "sources": ["RB-07"]}
    assert result.model == "claude-haiku-5-5" and result.tokens["input"] == 2000
    # 2,000 in at USD 0.10 and 100 out at USD 0.50 per million.
    assert result.cost == pytest.approx(0.00025)
    assert messages.calls[0]["model"] == "claude-haiku-5-5"


def test_a_refusal_or_a_cut_off_reply_is_never_read():
    for stop in ("refusal", "max_tokens"):
        sdk, _ = fake_sdk(stop=stop)
        with pytest.raises(llm.Refused):
            llm.complete("answer", "s", "u", SCHEMA, "runbook-assistant", "claude-haiku-5-5", "low", record=False, sdk=sdk)
    sdk, _ = fake_sdk(text="not json")
    with pytest.raises(llm.Refused):
        llm.complete("answer", "s", "u", SCHEMA, "runbook-assistant", "claude-haiku-5-5", "low", record=False, sdk=sdk)


def test_setting_values_are_refused():
    with pytest.raises(llm.Refused):
        llm.no_setting_values({"evidence": {"settings": {"DELIVERY_DAYS": "2"}}})
    llm.no_setting_values({"settings": {"DELIVERY_DAYS": None}})


def test_haiku_prices_rise_past_100000_prompt_tokens():
    short = llm.cost_of("claude-haiku-5-5", {"input": 100000, "output": 0, "cache_read": 0, "cache_write": 0})
    long = llm.cost_of("claude-haiku-5-5", {"input": 200000, "output": 1000, "cache_read": 0, "cache_write": 0})
    assert short == pytest.approx(0.01) and long == pytest.approx(0.1 + 0.0025)


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="needs gitleaks")
def test_a_key_in_the_input_never_reaches_the_api(monkeypatch):
    monkeypatch.undo()  # the real redact, with the real gitleaks
    text, removed = llm.redact(f"Why does {FAKE_KEY} fail to rotate?")
    assert removed == 1 and FAKE_KEY not in text and "[removed]" in text
    sdk, messages = fake_sdk()
    monkeypatch.setattr(llm, "send_usage", lambda *a: None)
    result = llm.complete("answer", "system", f"Question: {FAKE_KEY}", SCHEMA, "runbook-assistant",
                          "claude-haiku-5-5", "low", sdk=sdk)
    assert result.removed == 1
    assert FAKE_KEY not in json.dumps(messages.calls[0]["messages"])


def test_when_gitleaks_cant_run_nothing_is_sent(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("GITLEAKS", "/nonexistent/gitleaks")
    with pytest.raises(llm.Refused):
        llm.redact("anything")
