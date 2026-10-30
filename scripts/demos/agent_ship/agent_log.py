"""The coding agent's log: Claude Code's stream-json as readable blocks, and its Slack lines.

    claude -p ... --output-format stream-json | python3 scripts/demos/agent_ship/agent_log.py

agent_task.py feeds it the container's output line by line. For each event
it prints one readable block, the agent's words and the commands' output
verbatim:

    Model claude-opus-5-5 · turn limit 40 · 20 minutes · branch agent/ORD-91-12
    AWS role: release-demo-coding-agent, session coding-agent, until 14:05
    [read] services/orders-service/handler.py
    [edit] services/orders-service/handler.py
    $ git push origin HEAD:main
        remote: error: GH013: Repository rule violations found for refs/heads/main.
    Agent:
        <what it said>

A command's output shows its first 12 lines. Every secret value the run was
given (the GitHub token, the AWS session, the API key) is replaced by "****"
before anything prints, and then the patterns of gitleaks' default rules
for the tokens that matter here.

What the platform refused, the moment the tool's result shows it, becomes
one Slack line: "Refused by GitHub: push to main.", "Refused by AWS:
UpdateFunctionCode on orders-service-production.", "Refused by GitHub:
approving its own pull request #5." and the rest below. Nothing the agent
writes reaches Slack except its pull request's title.
"""

import json
import re
import sys
import time

OUTPUT_LINES = 12
REDACTED = "****"
# gitleaks' default rules for the token kinds an agent run can meet, as patterns.
PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),                      # github-pat, -app-token, -oauth, -refresh
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),                     # github-fine-grained-pat
    re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),  # aws-access-token
    re.compile(r"\bsk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{20,}"),       # anthropic-api-key, -admin-api-key
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),                      # slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # jwt
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),                    # private-key
    re.compile(r"(?i)(aws_secret_access_key|aws_session_token)\s*[=:]\s*\S+"),
]
GH013 = re.compile(r"GH013: Repository rule violations found for refs/(heads|tags)/(\S+?)\.?(?:\s|$)")
AWS_DENIED = re.compile(r"\((AccessDenied(?:Exception)?)\) when calling the (\w+) operation: User: \S+ is not "
                        r"authorized to perform: (\S+) on resource: (\S+)")
PULL_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
OWN_APPROVAL = re.compile(r"[Cc]an ?not approve your own pull request")
NOT_MERGEABLE = re.compile(r"not mergeable|prohibits the merge|approving review is required|Repository rule violations|"
                           r"merging is blocked|review required", re.I)
FILE_TOOLS = {"Read": "read", "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit"}


class Masker:
    def __init__(self, secrets):
        self.secrets = sorted((s for s in secrets if s and len(s) >= 8), key=len, reverse=True)

    def __call__(self, text):
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        for pattern in PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text


def short_resource(arn):
    """The name AWS printed, without its account and type: orders-service-production, release-demo-deploy-..."""
    rest = arn.split(":", 5)[5] if arn.count(":") >= 5 else arn
    for prefix in ("function:", "role/", "parameter/", "log-group:", "secret:", "table/", "user/"):
        if rest.startswith(prefix):
            return rest[len(prefix):].rstrip(".")
    return rest.rstrip(".")


def refusal(command, output, pull=None):
    """The Slack line for what a tool result shows the platform refused, or None."""
    command = command or ""
    found = GH013.search(output)
    if found and command.lstrip().startswith("git"):
        kind, ref = found.groups()
        return "Refused by GitHub: push to main." if (kind, ref) == ("heads", "main") else f"Refused by GitHub: push to {ref}."
    found = AWS_DENIED.search(output)
    if found:
        _, operation, action, resource = found.groups()
        return f"Refused by AWS: {action.split(':', 1)[-1] or operation} on {short_resource(resource)}."
    if re.search(r"\bgh\s+pr\s+review\b", command) and "--approve" in command and OWN_APPROVAL.search(output):
        return f"Refused by GitHub: approving its own pull request #{pr_number(command) or pull or '?'}."
    if re.search(r"\bgh\s+pr\s+merge\b", command) and NOT_MERGEABLE.search(output):
        return f"Refused by GitHub: merging pull request #{pr_number(command) or pull or '?'} without a review."
    return None


def pr_number(command):
    found = re.search(r"\bgh\s+pr\s+(?:review|merge)\s+#?(\d+)", command) or re.search(r"/pull/(\d+)", command)
    return int(found.group(1)) if found else None


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def relative(path, root="/work/release-demo/"):
    return path[len(root):] if path and path.startswith(root) else path


class Formatter:
    """Turns stream-json events into the log and the Slack lines. slack(lines) is told
    each time the lines change; title(n) gives a pull request's title from GitHub."""

    def __init__(self, secrets=(), out=sys.stdout, slack=None, title=None, limit=40, now=time.monotonic):
        self.mask = Masker(secrets)
        self.out, self.slack, self.title = out, slack, title
        self.limit, self.now = limit, now
        self.tools = {}       # tool_use id: (name, input)
        self.turns = 0
        self.lines = []       # Slack lines: refusals and the pull request, in order
        self.pull = None      # {"number", "url", "title"}
        self.result = None    # the final result event
        self.refusals = []
        self._last_post = 0.0

    def print(self, text=""):
        print(self.mask(text), file=self.out, flush=True)

    def header(self, model, limit, minutes, branch, role_line):
        self.print(f"Model {model} · turn limit {limit} · {minutes} minutes · branch {branch}")
        self.print(role_line)

    def feed(self, raw):
        try:
            event = json.loads(raw)
        except ValueError:
            if raw.strip():
                self.print(raw.rstrip("\n"))
            return
        kind = event.get("type")
        if kind == "assistant":
            self.turns += 1
            for part in event.get("message", {}).get("content", []):
                self.assistant_part(part)
            self.post(counter=True)
        elif kind == "user":
            for part in event.get("message", {}).get("content", []) if isinstance(event.get("message", {}).get(
                    "content"), list) else []:
                if part.get("type") == "tool_result":
                    self.tool_result(part)
        elif kind == "result":
            self.result = event

    def assistant_part(self, part):
        if part.get("type") == "text" and part.get("text", "").strip():
            self.print("Agent:")
            for line in part["text"].strip().splitlines():
                self.print(f"    {line}")
        elif part.get("type") == "tool_use":
            name, given = part.get("name"), part.get("input") or {}
            self.tools[part.get("id")] = (name, given)
            if name in FILE_TOOLS:
                self.print(f"[{FILE_TOOLS[name]}] {relative(given.get('file_path') or given.get('notebook_path'))}")
            elif name == "Bash":
                self.print(f"$ {given.get('command', '')}")

    def tool_result(self, part):
        name, given = self.tools.get(part.get("tool_use_id"), (None, {}))
        if name != "Bash":
            return
        output = text_of(part.get("content"))
        lines = output.splitlines()
        for line in lines[:OUTPUT_LINES]:
            self.print(f"    {line}")
        if len(lines) > OUTPUT_LINES:
            self.print(f"    ({len(lines) - OUTPUT_LINES} more lines)")
        command = given.get("command", "")
        found = PULL_URL.search(output)
        if found and re.search(r"\bgh\s+pr\s+create\b", command) and not self.pull:
            number = int(found.group(1))
            title = self.title(number) if self.title else ""
            self.pull = {"number": number, "url": found.group(0), "title": title}
            self.lines.append(f"Pull request #{number} opened: {title}")
            self.post()
        line = refusal(command, output, self.pull["number"] if self.pull else None)
        if line and line not in self.refusals:
            self.refusals.append(line)
            self.lines.append(line)
            self.post()

    def post(self, counter=False):
        """Slack, at once for a new line; the turn counter at most every 3 seconds."""
        if not self.slack:
            return
        if counter and self.now() - self._last_post < 3:
            return
        self._last_post = self.now()
        self.slack(list(self.lines), self.turns)

    def permission_denials(self):
        return len((self.result or {}).get("permission_denials") or [])


def end_line(formatter, total, timed_out_minutes=None):
    """The status message's last line."""
    if timed_out_minutes:
        return f"Stopped after {timed_out_minutes} minutes."
    if (formatter.result or {}).get("subtype") == "error_max_turns":
        return f"Stopped at the turn limit after {total}."
    if formatter.pull:
        return f"Finished in {total}. Pull request #{formatter.pull['number']} waits for review."
    return f"Finished in {total}. No pull request."


def usage(formatter):
    """Turns, tokens and cost from the final result event, for Splunk and Grafana (never Slack)."""
    result = formatter.result or {}
    tokens = result.get("usage") or {}
    return {"turns": result.get("num_turns", formatter.turns), "cost_usd": result.get("total_cost_usd"),
            "input_tokens": tokens.get("input_tokens"), "output_tokens": tokens.get("output_tokens"),
            "cache_read_tokens": tokens.get("cache_read_input_tokens"),
            "cache_write_tokens": tokens.get("cache_creation_input_tokens"),
            "permission_denials": formatter.permission_denials(), "result": result.get("subtype")}


def main():
    formatter = Formatter()
    for raw in sys.stdin:
        formatter.feed(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
