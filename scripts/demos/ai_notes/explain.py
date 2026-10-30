"""Video 13: the likely cause of a failed production check, after its rollback.

    python3 scripts/demos/ai_notes/explain.py --evidence .release/evidence.json --check .release/check-production.txt \\
        --failed-version 42 --rolled-back-to 1.4.0 --deployed-at <epoch> --rolled-back-at <epoch> \\
        --change-sys-id <id> --jira-key ORD-13 --slack-channel C... --slack-ts 1793.0001 --build-url <url>
    python3 scripts/demos/ai_notes/explain.py --from-files <folder>      the same task on saved sources, written nowhere

The ai-notes job runs it with KIND cause, only after the rollback, its check,
the Slack line, the change's close and the Jira comment. It reads, all after
the rollback:

- evidence.json and check-production.txt from the release build's archive;
- the failed version's CloudWatch log events, from the log streams whose names
  hold "[<version>]", between its deploy and 30 seconds after the rollback:
  error lines and their tracebacks first, then the rest, up to ai_notes.log_lines.
  It waits up to 20 seconds for the first error line, since CloudWatch can lag;
- the diff and the setting names per environment from evidence.json, and the
  Jira summary.

Claude answers with a cause, up to three quotes with their sources, and up to
two things it didn't check. There is no field for a fix. verify.py keeps a
quote only if it equals one whole line of its source; the cause may name only
settings, files and versions that are in the sources. The reply goes to the
release message's Slack thread, and the same text to the change as a work note
by ai.notes and to the Jira issue as a comment by Release Pipeline.
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

import notes
import verify
from llm import LLMError, ask, usage_event

TASK = "likely_cause"
LOG_WAIT_SECONDS = 20
AFTER_ROLLBACK_SECONDS = 30
ERROR_MARKERS = ("[ERROR]", "Traceback (most recent call last)", "Task timed out", "Runtime.", "Unhandled exception")
HEAD = ("*Likely cause of the failed production check*\n"
        "Written by {model} from this run's logs, diff and settings. Each quote checked word for word by script.")
LABELS = {"cloudwatch": "CloudWatch", "diff": "Diff", "settings": "Settings", "check": "Check production"}

SYSTEM = """\
A release failed its production check, and the pipeline has already rolled production back to the \
previous version. The user message holds what was read after the rollback, as JSON: the check's own output \
lines, the failed version's CloudWatch log lines, the code diff from the previous version, the setting names \
each environment defines (one line per environment, names only, never values), and the Jira summary. Use \
nothing else.

Return JSON that matches the schema:

- cause: one or two sentences on the likely cause of the failed check, as these lines show it. Name settings, \
files and versions exactly as the lines write them.
- evidence: up to three items that back the cause. source is cloudwatch, diff, settings or check, and quote \
is one whole line copied exactly from that source (a diff line without its leading + or -). A script \
compares each quote with its source character for character and drops any it can't find.
- not_checked: up to two short items on what these lines can't show.

It is a likely cause: you see only these lines. Don't propose a fix and don't say what anyone should do. \
The developer decides."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["cause", "evidence", "not_checked"],
    "properties": {
        "cause": {"type": "string"},
        "evidence": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["source", "quote"],
            "properties": {"source": {"type": "string", "enum": list(LABELS)}, "quote": {"type": "string"}}}},
        "not_checked": {"type": "array", "items": {"type": "string"}},
    },
}


# The CloudWatch lines.

def is_error(message):
    return any(marker in message for marker in ERROR_MARKERS)


def ordered_lines(events, limit):
    """Error events and their tracebacks first, then the rest, as lines, up to limit."""
    events = sorted(events, key=lambda event: event.get("timestamp", 0))
    first = [event for event in events if is_error(event["message"])]
    rest = [event for event in events if not is_error(event["message"])]
    lines = []
    for event in first + rest:
        lines += [line.rstrip() for line in event["message"].splitlines() if line.strip()]
    return lines[:limit]


def stream_prefixes(version, start, end):
    """Lambda names a version's log streams <yyyy/mm/dd>/[<version>]<id>, by the UTC day."""
    day = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).date()
    last = datetime.datetime.fromtimestamp(end, datetime.timezone.utc).date()
    prefixes = []
    while day <= last:
        prefixes.append(f"{day:%Y/%m/%d}/[{version}]")
        day += datetime.timedelta(days=1)
    return prefixes


def log_events(client, group, version, start, end, wait=LOG_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    """The failed version's events between start and end, waiting up to `wait`
    seconds for the first error line."""
    deadline = clock() + wait
    while True:
        streams = []
        for prefix in stream_prefixes(version, start, end):
            for page in client.get_paginator("describe_log_streams").paginate(logGroupName=group, logStreamNamePrefix=prefix):
                streams += [stream["logStreamName"] for stream in page["logStreams"]]
        events = []
        if streams:
            for page in client.get_paginator("filter_log_events").paginate(
                    logGroupName=group, logStreamNames=streams[:100], startTime=int(start * 1000), endTime=int(end * 1000)):
                events += [{"timestamp": event["timestamp"], "message": event["message"]} for event in page["events"]]
        if any(is_error(event["message"]) for event in events) or clock() >= deadline:
            return events
        sleep(2)


# The sources, the question and the reply.

def settings_lines(evidence):
    """One line per environment, as the settings source: "production: DELIVERY_DAYS, ENVIRONMENT"."""
    defined = (evidence.get("settings") or {}).get("defined") or {}
    return [f"{environment}: {', '.join(names)}" for environment, names in defined.items()]


def build_payload(evidence, check_lines, log_lines, failed):
    diff = evidence.get("diff") or {}
    return {
        "release": {"service": evidence["run"]["service"], "version": evidence["run"]["version"],
                    "jira_key": evidence["run"].get("jira_key"), "jira_summary": evidence["run"].get("jira_summary"),
                    "rolled_back_to": failed["rolled_back_to"]},
        "check": check_lines,
        "cloudwatch": {"function": failed["function"], "lambda_version": failed["lambda_version"], "lines": log_lines},
        "diff": {"from_version": diff.get("from_version"), "files": [f["file"] for f in diff.get("files") or []],
                 "text": diff.get("text") or diff.get("unavailable") or ""},
        "settings": settings_lines(evidence),
    }


def sources_of(sent):
    """The four sources as Claude got them, after gitleaks."""
    return {"cloudwatch": sent["cloudwatch"]["lines"], "diff": (sent["diff"]["text"] or "").splitlines(),
            "settings": sent["settings"], "check": sent["check"]}


def diff_file(sent, quote):
    """The file of the diff that holds the quoted line."""
    current = None
    for line in (sent["diff"]["text"] or "").splitlines():
        if line.startswith("--- "):
            current = None if line[4:] == "/dev/null" else line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            current = current if line[4:] == "/dev/null" else line[4:].removeprefix("b/")
        elif current and not line.startswith("@@") and verify.normal(line, "diff") == quote:
            return current
    return "the diff"


def bullet(item, sent):
    quote = item["quote"]
    if item["source"] == "cloudwatch":
        return f"• CloudWatch, {sent['cloudwatch']['function']} version {sent['cloudwatch']['lambda_version']}: `{quote}`"
    if item["source"] == "diff":
        return (f"• Diff {sent['diff']['from_version']} to {sent['release']['version']}, "
                f"{diff_file(sent, quote)}: `{quote}`")
    if item["source"] == "settings":
        environment, _, names = quote.partition(": ")
        return f"• Settings, {environment}: `{names}`"
    return f"• Check production: `{quote}`"


def no_cause(reason, build_url):
    link = f"{build_url.rstrip('/')}/artifact/.release/check-production.txt" if build_url else "the release build"
    return f"No likely cause from the logs this time: {reason}. The check's own output is in the build: {link}"


def reply(answer, build_url):
    """The Slack reply in its fixed form, or the "No likely cause" line."""
    sent, data = answer.sent, answer.data
    kept, dropped = verify.check_quotes(data, sources_of(sent))
    if not kept:
        return no_cause("none of its quotes was found in its source", build_url)
    known = verify.known_names({"sources": sources_of(sent), "release": sent["release"]}, extra_files=())
    unknown = verify.unknown_names(data["cause"], known)
    if unknown:
        return no_cause(f"the cause named {', '.join(unknown)}, which the sources don't hold", build_url)
    considered = len(kept) + len(dropped)
    found = f"Quotes found in their sources: {len(kept)} of {considered}."
    if dropped:
        found += " Dropped: " + "; ".join(f"{LABELS.get(item['source'], item['source'])}, not found" for item in dropped) + "."
    lines = [HEAD.format(model=answer.model), "", data["cause"].strip(), "", "*Evidence*",
             *[bullet(item, sent) for item in kept], found]
    checked = [item.strip().rstrip(".") for item in data["not_checked"][:2]
               if item.strip() and not verify.unknown_names(item, known)]
    if checked:
        lines += ["", f"*Not checked:* {'; '.join(checked)}."]
    return "\n".join(lines)


def explain(evidence, check_lines, log_lines, failed, build_url, client=None, scan=None):
    """(reply text, Answer or None)."""
    kwargs = {key: value for key, value in (("client", client), ("scan", scan)) if value}
    payload = build_payload(evidence, check_lines, log_lines, failed)
    try:
        answer = ask(TASK, evidence["run"]["service"], SYSTEM, SCHEMA, payload, **kwargs)
    except LLMError as error:
        return no_cause(str(error), build_url), None
    return reply(answer, build_url), answer


# Saved sources, for the tests and the history check.

def save_sources(folder, evidence, check_lines, events, failed):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (folder / "check-production.txt").write_text("\n".join(check_lines) + "\n")
    (folder / "cloudwatch.json").write_text(json.dumps(events, indent=1) + "\n")
    (folder / "failed.json").write_text(json.dumps(failed, indent=2) + "\n")


def load_sources(folder):
    folder = Path(folder)
    check = [line for line in (folder / "check-production.txt").read_text().splitlines() if line.strip()]
    return (notes.load_json(folder / "evidence.json"), check, notes.load_json(folder / "cloudwatch.json"),
            notes.load_json(folder / "failed.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-files", help="saved sources: evidence.json, check-production.txt, cloudwatch.json, failed.json")
    parser.add_argument("--evidence")
    parser.add_argument("--check")
    parser.add_argument("--failed-version")
    parser.add_argument("--rolled-back-to")
    parser.add_argument("--deployed-at", type=float)
    parser.add_argument("--rolled-back-at", type=float)
    parser.add_argument("--change-sys-id")
    parser.add_argument("--jira-key")
    parser.add_argument("--slack-channel")
    parser.add_argument("--slack-ts")
    parser.add_argument("--build-url", default="")
    args = parser.parse_args()

    if args.from_files:
        evidence, check_lines, events, failed = load_sources(args.from_files)
        limit = notes.settings(evidence["run"]["service"]).get("log_lines", 200)
        text, _ = explain(evidence, check_lines, ordered_lines(events, limit), failed, args.build_url)
        print(text)
        return 0

    started = time.time()
    evidence = notes.load_json(args.evidence)
    service_name = evidence["run"]["service"]
    config = notes.settings(service_name)
    if not config.get("likely_cause"):
        print(f"services.yaml doesn't ask for a likely cause for {service_name}.")
        return 0
    check_path = Path(args.check)
    check_lines = [line for line in check_path.read_text().splitlines() if line.strip()] if check_path.exists() else []
    function = notes.service(service_name)["environments"]["production"]["function"]
    failed = {"function": function, "lambda_version": args.failed_version, "rolled_back_to": args.rolled_back_to}
    try:
        from evidence import reader_session

        logs = reader_session().client("logs")
        events = log_events(logs, f"/aws/lambda/{function}", args.failed_version, args.deployed_at,
                            args.rolled_back_at + AFTER_ROLLBACK_SECONDS)
    except Exception as error:  # no role, no access: Claude gets the rest, and the reply can still say so
        print(f"CloudWatch: not read ({error.__class__.__name__}: {error})")
        events = []
    log_lines = ordered_lines(events, config.get("log_lines", 200))
    print(f"CloudWatch: {len(events)} events, {len(log_lines)} lines from version {args.failed_version}")
    text, answer = explain(evidence, check_lines, log_lines, failed, args.build_url)
    print(text)

    run = evidence["run"]
    sn = notes.notes_servicenow()
    writers = [("thread reply in Slack", lambda: notes.slack_thread_reply(args.slack_channel, args.slack_ts, text)),
               ("work note as ai.notes", lambda: notes.work_note(sn, args.change_sys_id, notes.plain(text))),
               (f"comment on {args.jira_key}", lambda: notes.jira_comment(args.jira_key, notes.plain(text)))]
    if not args.slack_channel or not args.slack_ts:
        writers.pop(0)
    if answer is not None:
        state = {"service": service_name, "version": run["version"], "jira_key": run.get("jira_key"),
                 "build": {"number": run.get("build"), "url": run.get("build_url")},
                 "change": {"number": run.get("change_number")}}
        writers.append(("tokens and cost to Splunk and Grafana",
                        lambda: notes.send_event(usage_event(answer, TASK, state, no_cause=text.startswith("No likely")))))
    failures = notes.each(writers)
    if args.rolled_back_at:
        print(f"Reply {time.time() - args.rolled_back_at:.0f}s after the rollback; this script took {time.time() - started:.0f}s.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
