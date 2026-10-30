"""The Jenkins job agent-task: the coding agent works on a Jira issue, on its own credentials.

    python3 scripts/demos/agent_ship/agent_task.py run      in agent-task, on the tools host

/agent <service> <Jira key> <note> in Slack starts the job as release-bot.
On the tools host this:

1. reads the issue as Release Pipeline: its summary, description and fix version;
2. clones main into a fresh folder, on a new local branch agent/<key>-<build>,
   with git's author set to the agent app's bot and gh as git's credential
   helper (the clone's own config: no token in a file);
3. gets the app's installation token and assumes release-demo-coding-agent
   as coding-agent for an hour, in this process, so neither reaches the log;
4. starts the coding-agent image, by its digest, as the user agent on the
   network release-demo-agent, which reaches the internet only, with the
   clone at /work/release-demo and nothing but GH_TOKEN, the AWS session,
   AWS_REGION and ANTHROPIC_API_KEY (anthropic/api-key);
5. runs claude -p with the model, turn limit and minutes from services.yaml,
   and the prompt below, which says what Sam asked and which tools it has,
   and nothing about what it may not do: whatever stops it is the platform;
6. prints the agent's stream as readable blocks (agent_log.py), keeps the
   Slack message up to date, comments on the issue, and at the end sends the
   run's turns, tokens and cost to Splunk and Grafana, never to Slack.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import agent
import agent_log
from common import ROOT, get_parameter, people, ssm_client

NETWORK = "release-demo-agent"
AGENT_ACCESS = ROOT / "scripts" / "demos" / "agent-access"
PROMPT = ("You are working in a clone of github.com/{repository} at /work/release-demo, on the branch {branch}. "
          "Jira {key}: {summary}. {description} Fix version: {fix_version}. Pull request titles start with the "
          "Jira key. {first_name}'s note: {note} You can use git, gh, aws, python3 and pytest; gh and aws are signed in.")


def agent_access():
    """Video 9's agent.py: the image, the role and the role line come from there."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_access", AGENT_ACCESS / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plain(document):
    """Jira's description (Atlassian document format) as plain text."""
    if not isinstance(document, dict):
        return document or ""
    if document.get("type") == "text":
        return document.get("text", "")
    parts = [plain(child) for child in document.get("content", [])]
    joiner = " " if document.get("type") in ("doc", "bulletList", "orderedList") else ""
    return joiner.join(part for part in parts if part).strip()


def issue(key):
    from jira import Jira

    fields = Jira().call("GET", f"/rest/api/3/issue/{key}?fields=summary,description,fixVersions")["fields"]
    return {"summary": fields["summary"], "description": plain(fields.get("description")),
            "fix_version": ", ".join(v["name"] for v in fields.get("fixVersions") or []) or "none"}


def comment(key, text):
    from jira import Jira, paragraph

    Jira().call("POST", f"/rest/api/3/issue/{key}/comment", {"body": paragraph(text)})


def requester(slack_id, everyone):
    for person in everyone.values():
        if slack_id and person.get("slack_user_id") == slack_id:
            return person
    return {"display_name": "someone in Slack"}


def prompt(repository, branch, key, found, note, first_name):
    description = found["description"].strip()
    if description and not description.endswith("."):
        description += "."
    note = note.strip()
    if note and not note.endswith((".", "!", "?")):
        note += "."
    return PROMPT.format(repository=repository, branch=branch, key=key, summary=found["summary"],
                         description=description, fix_version=found["fix_version"], first_name=first_name, note=note)


def head(key, summary, slack_id, note):
    mention = f"<@{slack_id}>" if slack_id else "the Jenkins UI"
    quoted = "\n".join(f"> {line}" for line in note.splitlines() or [""])
    return f"*Coding agent on {key}* · {summary} · asked by {mention}\n{quoted}"


def message(top, lines, turns, limit, links, end=None):
    body = [top, ""]
    if end is None:
        body.append(f"Working · turn {turns} of {limit}")
    body += lines
    if links:
        body.append(" · ".join(links))
    if end:
        body.append(end)
    return "\n".join(body)


class Slack:
    def __init__(self, token, channel, ts):
        self.token, self.channel, self.ts = token, channel, ts

    def update(self, text):
        if not (self.channel and self.ts):
            return
        from notify import slack

        slack("chat.update", self.token, channel=self.channel, ts=self.ts, text=text)


def clone(repository, folder, branch, author, email):
    run = lambda *args: subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    run("clone", "--quiet", f"https://github.com/{repository}.git", str(folder))
    # The clone's own config: gh hands git the token from GH_TOKEN, so no file holds it.
    for key, value in [("user.name", author), ("user.email", email),
                       ("credential.https://github.com.helper", "!gh auth git-credential")]:
        run("-C", str(folder), "config", key, value)
    run("-C", str(folder), "checkout", "--quiet", "-b", branch)
    subprocess.run(["chmod", "-R", "a+rwX", str(folder)], check=True)  # the container's user agent edits it


def container(image, folder, settings, text, env_names):
    return ["docker", "run", "--rm", "--name", f"agent-task-{os.environ.get('BUILD_NUMBER', int(time.time()))}",
            "--network", NETWORK, "--user", "agent", *[f for n in env_names for f in ("-e", n)],
            "-v", f"{folder}:/work/release-demo", "-w", "/work/release-demo", image,
            "claude", "-p", text, "--model", settings["model"], "--max-turns", str(settings["max_turns"]),
            "--output-format", "stream-json", "--verbose"]


def image_id(tag):
    found = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True)
    if found.returncode != 0:
        build = subprocess.run(["docker", "build", "-q", "-t", tag, "-f", "jenkins/toolchain/coding-agent.Dockerfile",
                                "jenkins/toolchain/coding-agent"], cwd=ROOT, capture_output=True, text=True)
        if build.returncode != 0:
            raise SystemExit(f"The coding agent's image didn't build: {build.stderr.strip()[-300:]}")
        found = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True)
    return found.stdout.strip()


def pull_title(ssm, repository):
    from common import github_api, github_app_token

    def title(number):
        token, _, _ = github_app_token(ssm)
        return github_api("GET", f"/repos/{repository}/pulls/{number}", token, scheme="token")["title"]

    return title


def send_event(ssm, body):
    from events import to_loki, to_splunk

    body = {"service": body["service"], "environment": None, "stage": "Coding agent", **body}
    return to_splunk(ssm, body), to_loki(ssm, body)


def run(env=os.environ):
    started_at = float(env.get("REQUESTED_AT") or time.time())
    key, name, note = env["JIRA_KEY"], env["SERVICE"], env.get("NOTE", "")
    build_number, build_url = env.get("BUILD_NUMBER", "0"), env.get("BUILD_URL", "")
    settings = agent.settings(name)
    ssm = ssm_client()
    everyone = people(ssm)
    asked = requester(env.get("REQUESTED_BY"), everyone)
    repository = get_parameter(ssm, "github/repository")
    found = issue(key)
    slack = Slack(get_parameter(ssm, "slack/bot-token"), env.get("SLACK_CHANNEL"), env.get("SLACK_TS"))
    top = head(key, found["summary"], env.get("REQUESTED_BY"), note)
    links = [f"<{build_url}|Build #{build_number}>"] if build_url else []
    slack.update(message(top, [], 0, settings["max_turns"], links))
    comment(key, f"Handed to the coding agent by {asked['display_name']}: {build_url}.")

    branch = f"agent/{key}-{build_number}"
    folder = Path(env.get("WORKSPACE", ROOT)) / "agent-work" / f"{key}-{build_number}"
    slug = get_parameter(ssm, "github/agent-app-slug")
    author, email = agent.bot_identity(slug)
    clone(repository, folder, branch, author, email)

    access = agent_access()
    github_token, _ = agent.token(ssm)
    credentials, expires = access.assume()
    api_key = get_parameter(ssm, access.CREDENTIAL)
    secrets = [github_token, api_key, *credentials.values()]
    names = ["GH_TOKEN", *access.PASSED]
    child_env = {"PATH": env.get("PATH", "/usr/bin:/bin"), "HOME": env.get("HOME", "/tmp"), "GH_TOKEN": github_token,
                 **credentials, "AWS_REGION": access.REGION, "ANTHROPIC_API_KEY": api_key}

    log_path = Path(env.get("WORKSPACE", ROOT)) / ".release" / "agent-log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = Tee(sys.stdout, log_path)
    state = {"lines": [], "turns": 0}

    def to_slack(lines, turns):
        state.update(lines=lines, turns=turns)
        slack.update(message(top, lines, turns, settings["max_turns"], pull_links(formatter, links)))

    formatter = agent_log.Formatter(secrets, out=out, slack=to_slack, title=pull_title(ssm, repository),
                                    limit=settings["max_turns"])
    formatter.header(settings["model"], settings["max_turns"], settings["minutes"], branch, access.role_line(expires, 200))
    text = prompt(repository, branch, key, found, note, asked["display_name"].split()[0])
    image = image_id(access.image_tag())
    process = subprocess.Popen(container(image, folder, settings, text, names), env=child_env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
    deadline = time.monotonic() + settings["minutes"] * 60
    timed_out, commented = None, False
    for raw in process.stdout:
        formatter.feed(raw)
        if formatter.pull and not commented:
            comment(key, f"Pull request #{formatter.pull['number']} opened by the coding agent: {formatter.pull['url']}")
            commented = True
        if time.monotonic() > deadline:
            subprocess.run(["docker", "kill", f"agent-task-{build_number}"], capture_output=True)
            timed_out = settings["minutes"]
            break
    process.wait()

    import run_state

    total = run_state.duration(time.time() - started_at)
    end = agent_log.end_line(formatter, total, timed_out)
    slack.update(message(top, formatter.lines, formatter.turns, settings["max_turns"], pull_links(formatter, links), end))
    out.write(end + "\n")
    figures = agent_log.usage(formatter)
    result = figures.pop("result")
    send_event(ssm, {"service": name, "jira_key": key, "build": build_number, "build_url": build_url,
                     "branch": branch, "pull_request": (formatter.pull or {}).get("number"),
                     "refusals": formatter.refusals, **figures, "result": "stopped" if timed_out else result or "ok"})
    print(json.dumps({"turns": figures["turns"], "cost_usd": figures["cost_usd"],
                      "permission_denials": figures["permission_denials"], "refusals": len(formatter.refusals)}))
    return 0


def pull_links(formatter, links):
    if not formatter.pull:
        return links
    return [*links, f"<{formatter.pull['url']}|Pull request #{formatter.pull['number']}>"]


class Tee:
    """The log to Jenkins' console and to .release/agent-log.txt, for the gitleaks check at the end."""

    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "w")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()


def main():
    if sys.argv[1:] != ["run"]:
        raise SystemExit(__doc__.splitlines()[2].strip())
    sys.stdout.reconfigure(line_buffering=True)
    return run()


if __name__ == "__main__":
    sys.exit(main())
