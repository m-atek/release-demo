"""One wrapper for every Claude call the platform makes (videos 12 and 13).

    from llm import ask
    answer = ask("change_summary", "orders-service", SYSTEM, SCHEMA, payload)
    answer.data, answer.model, answer.usage, answer.cost_usd, answer.removed

The model and the effort come from the service's ai_notes in services.yaml,
so changing either is a commit and a release of the platform. Every request:

- asks for structured output (output_config.format with the task's JSON
  schema); the reply is parsed with json.loads, and anything else is an error;
- sets the effort explicitly (Claude Opus 5.5 always thinks), max_tokens
  16000, a 90-second timeout and the SDK's two retries on 429 and 5xx;
- has the server-side fallback on (fallbacks "default") where the model has
  one, and the answer names the model that answered (response.model);
- checks stop_reason before reading any content;
- carries only text that passed gitleaks' default rules: each match becomes
  "[removed]" and is counted, and if gitleaks can't run, nothing is sent;
- refuses a payload that holds a settings value map: setting names only;
- puts the fixed system prompt first, marked for caching; the evidence follows.

It writes nowhere itself: its callers write the answer, and send
usage_event() through events.py to Splunk and Grafana Logs, never to Slack.
cost() prices a call from scripts/demos/ai_notes/prices.yaml, which the cost
check of video 18 shares.

AI_NOTES_REQUEST_LOG=<file> appends each request body as sent, one JSON line
each, so a test can show that no setting value leaves.
"""

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from common import ROOT, SSM_PREFIX, service, ssm_client

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


@lru_cache(maxsize=1)
def prices():
    return yaml.safe_load(PRICES.read_text())


def price_of(model):
    """USD per million tokens for a model, or LLMError if prices.yaml has none."""
    table = prices()["claude"]["per_million_tokens"]
    # A dated model name, should the API answer with one, takes its family's price.
    known = model if model in table else next((name for name in table if (model or "").startswith(name)), None)
    if known is None:
        raise LLMError(f"prices.yaml has no price for {model}")
    return table[known]


def cost(model, usage):
    """USD for one call's tokens on one model. usage holds input_tokens,
    output_tokens, cache_read_input_tokens and cache_creation_input_tokens."""
    price = price_of(model)
    return (usage.get("input_tokens", 0) * price["input"]
            + usage.get("output_tokens", 0) * price["output"]
            + (usage.get("cache_read_input_tokens") or 0) * price["cache_read"]
            + (usage.get("cache_creation_input_tokens") or 0) * price["cache_write"]) / 1_000_000


def task_settings(service_name, task):
    """(model, effort) for a task, from services.yaml's ai_notes of the service."""
    notes = service(service_name).get("ai_notes") or {}
    model, effort = notes.get("model"), (notes.get("effort") or {}).get(task)
    if not model or not effort:
        raise LLMError(f"services.yaml has no ai_notes model and effort for {task} in {service_name}")
    return model, effort


def has_value_map(payload):
    """True if anything in the payload maps setting names to values, such as
    {"DELIVERY_DAYS": "2"}: llm.py sends setting names, never their values."""
    if isinstance(payload, dict):
        named = [key for key in payload if isinstance(key, str) and SETTING_NAME.fullmatch(key)]
        if named and any(not isinstance(payload[key], (dict, list)) for key in named):
            return True
        return any(has_value_map(value) for value in payload.values())
    if isinstance(payload, list):
        return any(has_value_map(item) for item in payload)
    return False


def gitleaks_command():
    """The pinned gitleaks image through Docker, or, where Docker isn't
    running (the Mac), a gitleaks binary of the same version. None if neither."""
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker", "run", "--rm", "-i", "--network", "none", GITLEAKS_IMAGE, *GITLEAKS_ARGS]
    binary = shutil.which("gitleaks")
    if binary:
        version = subprocess.run([binary, "version"], capture_output=True, text=True).stdout.strip().lstrip("v")
        if version == GITLEAKS_VERSION:
            return [binary, *GITLEAKS_ARGS]
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


def redact(text, scan=gitleaks_findings):
    """The text with every gitleaks match replaced by [removed], and how many were."""
    removed = 0
    secrets = {finding.get("Secret") or finding.get("Match") for finding in scan(text)}
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        removed += text.count(secret)
        text = text.replace(secret, REMOVED)
    return text, removed


def api_key(ssm=None):
    ssm = ssm or ssm_client()
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + KEY_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    except Exception as error:  # missing name, no access, no network: all mean no key
        raise LLMError(f"the Anthropic API key couldn't be read ({error.__class__.__name__})") from error
    if value == "unset":
        raise LLMError(f"the Anthropic API key isn't set ({KEY_PARAMETER})")
    return value


def client_for(key):
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=RETRIES)


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


def log_request(body):
    path = os.environ.get("AI_NOTES_REQUEST_LOG")
    if path:
        with open(path, "a") as log:
            log.write(json.dumps(body, sort_keys=True) + "\n")


def send(client, body):
    """One request, with the SDK's errors turned into reasons a note can give."""
    import anthropic

    try:
        return client.beta.messages.create(**body)
    except anthropic.APITimeoutError as error:
        raise LLMError(f"the API didn't answer within {TIMEOUT_SECONDS:.0f} seconds") from error
    except anthropic.AuthenticationError as error:
        raise LLMError("the API refused the key") from error
    except anthropic.RateLimitError as error:
        raise LLMError("the API is rate-limiting this workspace (429)") from error
    except anthropic.APIStatusError as error:
        raise LLMError(f"the API answered {error.status_code}") from error
    except anthropic.APIConnectionError as error:
        raise LLMError("the API couldn't be reached") from error


def plain_usage(usage):
    return {name: getattr(usage, name, 0) or 0 for name in
            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


def read(response, requested):
    """(data, usage, cost, iterations) of a response, stop_reason checked first."""
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
    priced = iterations or [{"model": response.model, **usage}]
    total = sum(cost(entry["model"], entry) for entry in priced)
    return data, usage, total, iterations


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
    data, usage, total, iterations = read(response, model)
    return Answer(data=data, model=response.model, usage=usage, cost_usd=round(total, 6), removed=removed,
                  seconds=round(time.monotonic() - started, 1), request_id=getattr(response, "_request_id", "") or "",
                  iterations=iterations, sent=sent)


def usage_event(answer, task, state, **extra):
    """The event events.py sends for one call: tokens and cost, no text."""
    return {
        "build": state.get("build", {}).get("number"), "build_url": state.get("build", {}).get("url"),
        "service": state.get("service"), "version": state.get("version"), "environment": None,
        "change": state.get("change", {}).get("number"), "jira_key": state.get("jira_key"),
        "stage": "AI notes", "result": "ok", "event": "llm_call", "task": task, "model": answer.model,
        **answer.usage, "cost_usd": answer.cost_usd, "removed": answer.removed, "seconds": answer.seconds,
        "fallback": any(entry["model"] != answer.model for entry in answer.iterations), **extra,
    }
