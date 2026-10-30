"""One wrapper for every Claude call the platform makes (videos 12, 13 and 23).

    from llm import complete
    result = complete("answer", system=prompt, user=text, schema=schema, service="runbook-assistant",
                      model="claude-haiku-5-5", effort="low")
    result.data, result.model, result.cost, result.removed

- Model and effort: per task from the service's ai_notes in services.yaml
  (model, effort.<task>), so a model change is a commit. Or the caller's,
  accepted only if services.yaml lists the model under the service's
  evaluation.answer_models (demo 23: the assistant's package names its own
  model, so a model change is a release of the assistant).
- Structured output: output_config.format with the task's JSON schema; the
  reply is parsed with json.loads, and anything else is an error.
  stop_reason is checked before any content is read.
- Effort is always set: Claude Opus 5.5 and Haiku 5.5 default to medium.
  max_tokens 16000, a 90-second timeout, the SDK's two retries on 429 and 5xx.
- The server-side fallback is on (fallbacks "default") where the model has
  one, Sonnet 5.5 and Opus 5.5; Haiku 5.5 has none. The result names the
  model that answered (response.model).
- Every input text passes gitleaks' default rules first; each match becomes
  "[removed]" and is counted, so a caller can refuse instead. If gitleaks
  can't run, nothing is sent.
- Setting names only, never values: a payload with a settings value map is
  refused.
- The fixed system prompt goes first, marked for caching; the varying text
  follows.
- Per call: input, output and cache tokens and the cost, from
  scripts/llm_prices.yaml, sent with events.py to Splunk and Grafana Logs.
  Never to Slack. It writes nowhere else: its callers do.

The key is anthropic/api-key in SSM; the workspace's spend limit caps its cost.
"""

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from common import ROOT, fail, get_parameter, service, ssm_client

MAX_TOKENS = 16000
TIMEOUT_SECONDS = 90
RETRIES = 2
REMOVED = "[removed]"
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
# Models with a server-side fallback (Claude API only). Claude Haiku 5.5 has none.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}
PRICES = ROOT / "scripts" / "llm_prices.yaml"
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_SHA256 = {
    "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
}


class Refused(Exception):
    """A call llm.py won't make, or a reply it won't use. The message says why."""


@dataclass
class Result:
    data: dict
    model: str
    tokens: dict
    cost: float
    removed: int = 0
    seconds: float = 0.0
    extra: dict = field(default_factory=dict)


# Model and effort

def choose(task, service_name, model=None, effort=None):
    """(model, effort) for a task: the caller's, from the allowlist, or the service's ai_notes."""
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


# Secrets out of every input

def gitleaks_binary():
    """$GITLEAKS, gitleaks on the PATH, or the pinned release, downloaded once and checked by its SHA-256."""
    if os.environ.get("GITLEAKS"):
        return os.environ["GITLEAKS"]
    if shutil.which("gitleaks"):
        return shutil.which("gitleaks")
    system = {"Linux": "linux", "Darwin": "darwin"}[platform.system()]
    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    target = f"{system}_{machine}"
    cache = Path(os.environ.get("RELEASE_DEMO_CACHE") or ROOT / ".local" / "cache") / f"gitleaks-{GITLEAKS_VERSION}"
    binary = cache / target / "gitleaks"
    if binary.exists():
        return str(binary)
    url = (f"https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}/"
           f"gitleaks_{GITLEAKS_VERSION}_{target}.tar.gz")
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != GITLEAKS_SHA256.get(target):
        raise Refused(f"{url} doesn't have the SHA-256 llm.py pins. Not running it, so nothing is sent.")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        data = tar.extractfile("gitleaks").read()
    binary.parent.mkdir(parents=True, exist_ok=True)
    partial = binary.with_name(f"gitleaks.{os.getpid()}.partial")
    partial.write_bytes(data)
    partial.chmod(0o755)
    partial.replace(binary)
    return str(binary)


def redact(text):
    """(text with every gitleaks finding replaced by [removed], how many). Raises Refused if gitleaks can't run."""
    if not text:
        return text, 0
    try:
        binary = gitleaks_binary()
        with tempfile.TemporaryDirectory(prefix="llm-gitleaks-") as folder:
            report = Path(folder) / "report.json"
            result = subprocess.run([binary, "stdin", "--no-banner", "--no-color", "--log-level", "error",
                                     "--report-format", "json", "--report-path", str(report), "--exit-code", "0"],
                                    input=text, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 or not report.exists():
                raise Refused(f"gitleaks failed ({result.returncode}): {result.stderr.strip()[:200]}")
            findings = json.loads(report.read_text() or "[]")
    except (OSError, subprocess.TimeoutExpired, KeyError, ValueError) as error:
        raise Refused(f"gitleaks can't run ({error.__class__.__name__}), so nothing is sent.")
    secrets = sorted({f.get("Secret") or f.get("Match") for f in findings if f.get("Secret") or f.get("Match")},
                     key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, REMOVED)
    return text, len(findings)


def no_setting_values(payload):
    """Setting names only: a settings map with values is refused."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "settings" and isinstance(value, dict) and any(v not in (None, "") for v in value.values()):
                raise Refused("The payload carries setting values; llm.py sends setting names only.")
            no_setting_values(value)
    elif isinstance(payload, list):
        for item in payload:
            no_setting_values(item)


# Cost

_prices = {}


def cost_of(model, tokens):
    if not _prices:
        _prices.update(yaml.safe_load(PRICES.read_text())["models"])
    price = dict(_prices.get(model) or {})
    if not price:
        return 0.0
    prompt = tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
    if price.get("long_prompt") and prompt > price["long_prompt"]["over"]:
        price.update(price["long_prompt"])
    return round((tokens["input"] * price["input"] + tokens["output"] * price["output"]
                  + tokens["cache_read"] * price["cache_read"] + tokens["cache_write"] * price["cache_write"]) / 1e6, 6)


# The call

_client, _lock = {}, threading.Lock()


def client():
    """The SDK client, made once per process with the platform's key."""
    with _lock:
        if "anthropic" not in _client:
            import anthropic

            _client["anthropic"] = anthropic.Anthropic(api_key=get_parameter(ssm_client(), "anthropic/api-key"),
                                                       timeout=TIMEOUT_SECONDS, max_retries=RETRIES)
        return _client["anthropic"]


def request(model, effort, system, user, schema):
    """The Messages API request: system prompt first and cached, then the varying text."""
    params = {"model": model, "max_tokens": MAX_TOKENS,
              "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
              "messages": [{"role": "user", "content": user}],
              "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}}}
    if model in FALLBACK_MODELS:
        params.update(FALLBACK)
    return params


def parse(response):
    """The reply's JSON, after checking why it stopped."""
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
    response = (sdk.beta.messages.create(**params) if "betas" in params else sdk.messages.create(**params))
    data = parse(response)
    tokens = usage(response)
    result = Result(data=data, model=getattr(response, "model", model), tokens=tokens,
                    cost=cost_of(getattr(response, "model", model), tokens), removed=removed,
                    seconds=round(time.monotonic() - started, 2))
    if record:
        send_usage(task, service, result, effort)
    return result


def send_usage(task, service_name, result, effort):
    """The call's tokens and cost, as one event to Splunk and Grafana Logs. Never fails the caller."""
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
    fail("llm.py is a module: from llm import complete")
