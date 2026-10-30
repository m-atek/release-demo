"""One wrapper for every Claude call the platform makes (videos 12, 13 and 23).

    from llm import ask                                   videos 12 and 13, the AI notes
    answer = ask("change_summary", "orders-service", SYSTEM, SCHEMA, payload)
    answer.data, answer.model, answer.usage, answer.cost_usd, answer.removed

    from llm import complete                              demo 23, the runbook assistant
    result = complete("answer", system=prompt, user=text, schema=schema, service="runbook-assistant",
                      model="claude-haiku-5-5", effort="low")
    result.data, result.model, result.tokens, result.cost, result.removed

The model and the effort come from the service's ai_notes in services.yaml
(model, effort.<task>), so changing either is a commit and a release of the
platform. complete() may also take the caller's model, accepted only if
services.yaml lists it under the service's evaluation.answer_models (demo 23:
the assistant's package names its own model, so a model change is a release
of the assistant). Every request:

- asks for structured output (output_config.format with the task's JSON
  schema); the reply is parsed with json.loads, and anything else is an error;
- sets the effort explicitly (Claude Opus 5.5 always thinks), max_tokens
  16000, a 90-second timeout and the SDK's two retries on 429 and 5xx;
- has the server-side fallback on (fallbacks "default") where the model has
  one, and the answer names the model that answered (response.model);
  Claude Haiku 5.5 has none;
- checks stop_reason before reading any content;
- carries only text that passed gitleaks' default rules: each match becomes
  "[removed]" and is counted, and if gitleaks can't run, nothing is sent;
- refuses a payload that holds a settings value map: setting names only;
- puts the fixed system prompt first, marked for caching; the varying text
  follows.

It writes nowhere itself: its callers write the answer, and the call's
tokens and cost go to Splunk and Grafana Logs (usage_event() through
events.py for the AI notes, send_usage() for complete()), never to Slack.
The prices are scripts/demos/ai_notes/prices.yaml's, dated, which video 18's
cost check shares. The key is anthropic/api-key in SSM; the workspace's spend
limit caps its cost.

AI_NOTES_REQUEST_LOG=<file> appends each request body as sent, one JSON line
each, so a test can show that no setting value leaves.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from common import ROOT, SSM_PREFIX, fail, service, ssm_client

PRICES = ROOT / "scripts" / "demos" / "ai_notes" / "prices.yaml"
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 90.0
RETRIES = 2
KEY_PARAMETER = "anthropic/api-key"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The models whose requests take fallbacks: "default" on the Claude API.
# Claude Haiku 5.5 has no server-side fallback.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_IMAGE = ("zricethezav/gitleaks:v8.30.1"
                  "@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f")
GITLEAKS_ARGS = ["stdin", "--no-banner", "--report-format", "json", "--report-path", "-",
                 "--log-level", "error", "--exit-code", "0"]
REMOVED = "[removed]"
SETTING_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


class LLMError(Exception):
    """Why there is no answer, in words a note can carry: "the API didn't answer within 90 seconds"."""


# Demo 23's name for the same thing: a call llm.py won't make, or a reply it won't use.
Refused = LLMError


@dataclass
class Answer:
    data: dict
    model: str
    usage: dict
    cost_usd: float
    removed: int
    seconds: float
    request_id: str = ""
    iterations: list = field(default_factory=list)
    sent: dict = field(default_factory=dict)  # the payload as Claude got it, after gitleaks


@dataclass
class Result:
    data: dict
    model: str
    tokens: dict
    cost: float
    removed: int = 0
    seconds: float = 0.0
    extra: dict = field(default_factory=dict)


# Prices

@lru_cache(maxsize=1)
def prices():
    return yaml.safe_load(PRICES.read_text())


def price_of(model):
    """USD per million tokens for a model, or LLMError if prices.yaml has none."""
    table = prices()["claude"]["per_million_tokens"]
    # A dated model name, should the API answer with one, takes its family's price.
    known = model if model in table else max((name for name in table if (model or "").startswith(name)), key=len,
                                             default=None)
    if known is None:
        raise LLMError(f"prices.yaml has no price for {model}")
    return table[known]


def priced(price, input_tokens, output_tokens, cache_read, cache_write):
    """USD for one call's tokens at one model's price. A prompt over long_prompt's
    threshold (Claude Haiku 5.5's 100,000 tokens) pays its higher prices."""
    long_prompt = price.get("long_prompt")
    if long_prompt and input_tokens + cache_read + cache_write > long_prompt["over"]:
        price = {**price, **long_prompt}
    return (input_tokens * price["input"] + output_tokens * price["output"]
            + cache_read * price["cache_read"] + cache_write * price["cache_write"]) / 1_000_000


def cost(model, usage):
    """USD for one call's tokens on one model. usage holds input_tokens,
    output_tokens, cache_read_input_tokens and cache_creation_input_tokens."""
    return priced(price_of(model), usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                  usage.get("cache_read_input_tokens") or 0, usage.get("cache_creation_input_tokens") or 0)


def cost_of(model, tokens):
    """complete()'s cost: tokens holds input, output, cache_read and cache_write. 0.0 for a
    model prices.yaml doesn't name, so a cost never stops an evaluation."""
    try:
        price = price_of(model)
    except LLMError:
        return 0.0
    return round(priced(price, tokens["input"], tokens["output"], tokens["cache_read"], tokens["cache_write"]), 6)


# Model and effort

def task_settings(service_name, task):
    """(model, effort) for a task, from services.yaml's ai_notes of the service."""
    notes = service(service_name).get("ai_notes") or {}
    model, effort = notes.get("model"), (notes.get("effort") or {}).get(task)
    if not model or not effort:
        raise LLMError(f"services.yaml has no ai_notes model and effort for {task} in {service_name}")
    return model, effort


def choose(task, service_name, model=None, effort=None):
    """(model, effort) for complete(): the caller's, from the allowlist, or the service's ai_notes."""
    config = service(service_name)
    if model:
        allowed = (config.get("evaluation") or {}).get("answer_models") or []
        if model not in allowed:
            raise Refused(f"{model} isn't in {service_name}'s evaluation.answer_models in services.yaml "
                          f"({', '.join(allowed) or 'none'}).")
        return model, effort or "low"
    notes = config.get("ai_notes") or {}
    if not notes.get("model"):
        raise Refused(f"{service_name} has no ai_notes.model in services.yaml, and the caller named none.")
    return notes["model"], (notes.get("effort") or {}).get(task) or effort or "medium"


# Setting names only, never values

def has_value_map(payload):
    """True if anything in the payload maps setting names to values, such as
    {"DELIVERY_DAYS": "2"}: llm.py sends setting names, never their values.
    A name with nothing (None or "") carries no value."""
    if isinstance(payload, dict):
        named = [key for key in payload if isinstance(key, str) and SETTING_NAME.fullmatch(key)]
        if named and any(not isinstance(payload[key], (dict, list)) and payload[key] not in (None, "")
                         for key in named):
            return True
        return any(has_value_map(value) for value in payload.values())
    if isinstance(payload, list):
        return any(has_value_map(item) for item in payload)
    return False


def no_setting_values(payload):
    """complete()'s check: a settings map with values, or any value map, is refused."""
    def settings_with_values(value):
        if isinstance(value, dict):
            return any((key == "settings" and isinstance(inner, dict)
                        and any(v not in (None, "") for v in inner.values())) or settings_with_values(inner)
                       for key, inner in value.items())
        if isinstance(value, list):
            return any(settings_with_values(item) for item in value)
        return False

    if has_value_map(payload) or settings_with_values(payload):
        raise Refused("The payload carries setting values; llm.py sends setting names only.")


# Secrets out of every input

def gitleaks_command():
    """How gitleaks runs here: $GITLEAKS if set; else a local gitleaks of the
    pinned version (the Mac); else the pinned image through Docker (the tools
    host). None if none of them can."""
    if os.environ.get("GITLEAKS"):
        return [os.environ["GITLEAKS"], *GITLEAKS_ARGS]
    binary = shutil.which("gitleaks")
    if binary:
        version = subprocess.run([binary, "version"], capture_output=True, text=True).stdout.strip().lstrip("v")
        if version == GITLEAKS_VERSION:
            return [binary, *GITLEAKS_ARGS]
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker", "run", "--rm", "-i", "--network", "none", GITLEAKS_IMAGE, *GITLEAKS_ARGS]
    return None


def gitleaks_findings(text):
    """What gitleaks' default rules find in the text: [{"Secret", "Match", "RuleID"}]."""
    command = gitleaks_command()
    if command is None:
        raise LLMError(f"gitleaks can't run here (no Docker, no gitleaks {GITLEAKS_VERSION}), so nothing was sent")
    try:
        result = subprocess.run(command, input=text, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LLMError(f"gitleaks didn't run ({error.__class__.__name__}), so nothing was sent") from error
    try:
        findings = json.loads(result.stdout or "null")
    except ValueError:
        findings = None
    if result.returncode != 0 or not isinstance(findings, list):
        raise LLMError(f"gitleaks failed (exit {result.returncode}), so nothing was sent")
    return findings


def redact(text, scan=None):
    """The text with every gitleaks match replaced by [removed], and how many were.
    LLMError (Refused) if gitleaks can't run: then nothing is sent."""
    if not text:
        return text, 0
    removed = 0
    secrets = {finding.get("Secret") or finding.get("Match") for finding in (scan or gitleaks_findings)(text)}
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        removed += text.count(secret)
        text = text.replace(secret, REMOVED)
    return text, removed


# The call

def api_key(ssm=None):
    ssm = ssm or ssm_client()
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + KEY_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    except Exception as error:  # missing name, no access, no network: all mean no key
        raise LLMError(f"the Anthropic API key couldn't be read ({error.__class__.__name__})") from error
    if value == "unset":
        raise LLMError(f"the Anthropic API key isn't set ({KEY_PARAMETER})")
    return value


def sdk():
    try:
        import anthropic
    except ImportError as error:  # a host that hasn't installed requirements-dev.txt since this was added
        raise LLMError("the Claude SDK isn't installed here (requirements-dev.txt)") from error
    return anthropic


def client_for(key):
    return sdk().Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=RETRIES)


_client, _lock = {}, threading.Lock()


def client():
    """complete()'s SDK client, made once per process with the platform's key."""
    with _lock:
        if "anthropic" not in _client:
            _client["anthropic"] = client_for(api_key())
        return _client["anthropic"]


def request_body(model, effort, system, schema, text, max_tokens=MAX_TOKENS):
    """The keyword arguments of client.beta.messages.create for one task."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": text}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }
    if model in FALLBACK_MODELS:
        body.update(betas=[FALLBACK_BETA], fallbacks="default")
    return body


def request(model, effort, system, user, schema):
    """request_body() in complete()'s order of arguments."""
    return request_body(model, effort, system, schema, user)


def log_request(body):
    path = os.environ.get("AI_NOTES_REQUEST_LOG")
    if path:
        with open(path, "a") as log:
            log.write(json.dumps(body, sort_keys=True) + "\n")


def api_error_reason(error):
    """The SDK's error in words a note can give, or None for any other error."""
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(error, anthropic.APITimeoutError):
        return f"the API didn't answer within {TIMEOUT_SECONDS:.0f} seconds"
    if isinstance(error, anthropic.AuthenticationError):
        return "the API refused the key"
    if isinstance(error, anthropic.RateLimitError):
        return "the API is rate-limiting this workspace (429)"
    if isinstance(error, anthropic.APIStatusError):
        return f"the API answered {error.status_code}"
    if isinstance(error, anthropic.APIConnectionError):
        return "the API couldn't be reached"
    return None


def send(client, body):
    """One request, through the Messages API's beta route (the fallback is a beta), with
    the SDK's errors turned into reasons a note can give."""
    try:
        return client.beta.messages.create(**body)
    except Exception as error:
        reason = api_error_reason(error)
        if reason is None:
            raise
        raise LLMError(reason) from error


def plain_usage(usage):
    return {name: getattr(usage, name, 0) or 0 for name in
            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


def read(response, requested):
    """ask()'s reading of a response: (data, usage, cost, iterations), stop_reason checked first."""
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise LLMError(f"Claude declined the request{f' ({category})' if category else ''}")
    if response.stop_reason == "max_tokens":
        raise LLMError(f"the answer ran past {MAX_TOKENS} tokens")
    if response.stop_reason != "end_turn":
        raise LLMError(f"the answer stopped early ({response.stop_reason})")
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        data = json.loads(text)
    except ValueError as error:
        raise LLMError("the answer wasn't the JSON the schema asks for") from error
    if not isinstance(data, dict):
        raise LLMError("the answer wasn't the JSON the schema asks for")
    usage = plain_usage(response.usage)
    # usage.iterations has every attempt when a fallback ran; the top-level
    # usage covers only the attempt that answered.
    iterations = [{"model": getattr(entry, "model", None) or requested, **plain_usage(entry)}
                  for entry in (getattr(response.usage, "iterations", None) or [])]
    priced_attempts = iterations or [{"model": response.model, **usage}]
    total = sum(cost(entry["model"], entry) for entry in priced_attempts)
    return data, usage, total, iterations


def parse(response):
    """complete()'s reading of a reply: its JSON, after checking why it stopped."""
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise Refused(f"Claude declined ({category or 'no category'}).")
    if response.stop_reason == "max_tokens":
        raise Refused(f"The reply was cut off at {MAX_TOKENS} tokens.")
    texts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    if not texts:
        raise Refused(f"No text in the reply (stop reason {response.stop_reason}).")
    try:
        return json.loads(texts[0])
    except ValueError:
        raise Refused("The reply isn't the JSON the schema asks for.")


def usage(response):
    found = response.usage
    return {"input": getattr(found, "input_tokens", 0) or 0, "output": getattr(found, "output_tokens", 0) or 0,
            "cache_read": getattr(found, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(found, "cache_creation_input_tokens", 0) or 0}


def ask(task, service_name, system, schema, payload, client=None, scan=gitleaks_findings, ssm=None):
    """Claude's structured answer to the payload, or LLMError saying why there is none."""
    if has_value_map(payload):
        raise LLMError("the payload held setting values, so nothing was sent")
    model, effort = task_settings(service_name, task)
    text, removed = redact(json.dumps(payload, indent=1, sort_keys=True), scan)
    try:
        sent = json.loads(text)
    except ValueError as error:
        raise LLMError("gitleaks' replacements left the payload unreadable, so nothing was sent") from error
    client = client or client_for(api_key(ssm))
    body = request_body(model, effort, system, schema, text)
    log_request(body)
    started = time.monotonic()
    response = send(client, body)
    data, usage_, total, iterations = read(response, model)
    return Answer(data=data, model=response.model, usage=usage_, cost_usd=round(total, 6), removed=removed,
                  seconds=round(time.monotonic() - started, 1), request_id=getattr(response, "_request_id", "") or "",
                  iterations=iterations, sent=sent)


def complete(task, system, user, schema, service, model=None, effort=None, record=True, sdk=None):
    """One structured call. user is text, or a dict sent as JSON. Returns a Result."""
    model, effort = choose(task, service, model, effort)
    no_setting_values(user)
    text = user if isinstance(user, str) else json.dumps(user, sort_keys=True, indent=1)
    text, removed = redact(text)
    system, removed_system = redact(system)
    removed += removed_system
    started = time.monotonic()
    sdk = sdk or client()
    params = request(model, effort, system, text, schema)
    log_request(params)
    response = send(sdk, params)
    data = parse(response)
    tokens = usage(response)
    answered = getattr(response, "model", model)
    result = Result(data=data, model=answered, tokens=tokens, cost=cost_of(answered, tokens), removed=removed,
                    seconds=round(time.monotonic() - started, 2))
    if record:
        send_usage(task, service, result, effort)
    return result


# What a call used, never to Slack

def usage_event(answer, task, state, **extra):
    """The event events.py sends for one ask(): tokens and cost, no text."""
    return {
        "build": state.get("build", {}).get("number"), "build_url": state.get("build", {}).get("url"),
        "service": state.get("service"), "version": state.get("version"), "environment": None,
        "change": state.get("change", {}).get("number"), "jira_key": state.get("jira_key"),
        "stage": "AI notes", "result": "ok", "event": "llm_call", "task": task, "model": answer.model,
        **answer.usage, "cost_usd": answer.cost_usd, "removed": answer.removed, "seconds": answer.seconds,
        "fallback": any(entry["model"] != answer.model for entry in answer.iterations), **extra,
    }


def send_usage(task, service_name, result, effort):
    """complete()'s tokens and cost, as one event to Splunk and Grafana Logs. Never fails the caller."""
    try:
        import events
        import run_state

        state = run_state.load()
        body = {"kind": "llm", "task": task, "service": service_name, "model": result.model, "effort": effort,
                "tokens": result.tokens, "cost_usd": result.cost, "removed": result.removed,
                "seconds": result.seconds, "build": (state.get("build") or {}).get("number"),
                "version": state.get("version"), "environment": None, "stage": f"llm {task}", "result": "ok"}
        ssm = ssm_client()
        events.to_splunk(ssm, body)
        events.to_loki(ssm, body)
    except (Exception, SystemExit):
        pass


if __name__ == "__main__":
    fail("llm.py is a module: from llm import ask, or from llm import complete")
