"""Video 12: the summary for the change manager, on a normal change.

    python3 scripts/demos/ai_notes/summarize.py --evidence .release/evidence.json --change-sys-id <id>
    python3 scripts/demos/ai_notes/summarize.py --evidence <file> --dry-run       the note, written nowhere

The ai-notes job runs it with KIND summary. It sends evidence.json (setting
names only) to Claude through scripts/llm.py, checks the answer with
verify.py, and writes one work note on the change as the ServiceNow user
ai.notes, in the fixed form below, with evidence.json attached. Only the
angle-bracket parts are Claude's sentences or the evidence's values. If one
statement doesn't match the evidence, or the API fails, times out or
declines, the note says there is no summary and why, and evidence.json is
attached either way. Tokens and cost go to Splunk and Grafana Logs.
"""

import argparse
import sys

import notes
import verify
from llm import LLMError, ask, usage_event

TASK = "change_summary"
HEAD = ("Summary for the change manager\n"
        "Written by {model} from this run's evidence. Every number checked against the attachments by script.")
SOURCES = {"tests": ("Tests", "junit.xml"), "quality_gate": ("Quality gate", "SonarQube"),
           "scan": ("Scan", "grype.json"), "package": ("Package", "build-info.json")}

SYSTEM = """\
You write the summary a change manager reads in ServiceNow before deciding whether a release goes to \
production. The user message is this run's evidence.json: what the release pipeline recorded about the \
run, its results, the versions, the code diff and the setting names each environment defines and the code \
reads. It holds setting names only, never their values. Use nothing else, and don't guess beyond it.

Write short, plain sentences for a reader who manages changes and doesn't read code. Return JSON that \
matches the schema:

- what_changes: one or two sentences on what this version changes for the service and its users, from the \
diff and the Jira summary. names lists every setting name, file name and version your sentences use.
- results: one sentence each for tests, quality_gate, scan and package, on what the evidence shows.
- not_checked: one item for each entry in findings, and none for anything else. finding is the entry's id \
(F1, F2 and so on). Its sentence says that this version reads the setting, by its exact name, and that \
the environment named in missing_in doesn't define it.

In every item, numbers lists each number its sentence states, with the dotted key in evidence.json whose \
value it is, such as results.tests.passed or results.scan.critical. Versions aren't numbers for this list. \
A script checks every number, setting name, file name and version against evidence.json and withholds the \
whole summary if one doesn't match, so copy them exactly. Don't recommend approving or rejecting, and don't \
suggest a fix: the change manager decides."""

NUMBERS = {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["value", "key"],
                                      "properties": {"value": {"type": "number"}, "key": {"type": "string"}}}}
ITEM = {"type": "object", "additionalProperties": False, "required": ["sentence", "numbers"],
        "properties": {"sentence": {"type": "string"}, "numbers": NUMBERS}}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["what_changes", "results", "not_checked"],
    "properties": {
        "what_changes": {"type": "object", "additionalProperties": False, "required": ["sentence", "names", "numbers"],
                         "properties": {"sentence": {"type": "string"},
                                        "names": {"type": "array", "items": {"type": "string"}}, "numbers": NUMBERS}},
        "results": {"type": "object", "additionalProperties": False, "required": list(SOURCES),
                    "properties": {key: ITEM for key in SOURCES}},
        "not_checked": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["finding", "sentence", "numbers"],
            "properties": {"finding": {"type": "string"}, "sentence": {"type": "string"}, "numbers": NUMBERS}}},
    },
}


def joined(names, word):
    """staging; staging and production; a, b and c."""
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" {word} " + names[-1]


def found_line(finding):
    """The script's own line under Claude's sentence."""
    defined = f"Defined in {joined(finding['defined_in'], 'and')}" if finding["defined_in"] else "Defined in no environment"
    return (f"Found by evidence.py: {finding['file']} line {finding['line']} reads it with no default. "
            f"{defined}, not in {joined(finding['missing_in'], 'or')}.")


def note(answer, evidence):
    """The work note in its fixed form."""
    data = answer.data
    lines = [HEAD.format(model=answer.model), "", "What changes", data["what_changes"]["sentence"].strip(), "", "Results"]
    for key, (label, source) in SOURCES.items():
        lines.append(f"- {label}: {data['results'][key]['sentence'].strip()} [{source}]")
    findings = {finding["id"]: finding for finding in evidence.get("findings") or []}
    if not findings:
        lines += ["", "Not checked by the pipeline: evidence.py found nothing."]
        return "\n".join(lines)
    lines += ["", "Not checked by the pipeline"]
    by_finding = {item["finding"]: item for item in data["not_checked"]}
    for finding_id, finding in findings.items():
        lines.append(f"- {by_finding[finding_id]['sentence'].strip()}")
        lines.append(f"  {found_line(finding)}")
    return "\n".join(lines)


def withheld(failures):
    count = len(failures)
    return (f"No summary for this change. {count} statement{'s' if count != 1 else ''} didn't match the evidence: "
            f"{'; '.join(failures)}. The evidence is attached as usual.")


def failed(reason):
    return f"No summary for this change: {reason}. The evidence is attached as usual."


def summarize(evidence, service_name, client=None, scan=None):
    """(note text, Answer or None): the summary, or the note that says why there is none."""
    kwargs = {"client": client} if client else {}
    if scan:
        kwargs["scan"] = scan
    try:
        answer = ask(TASK, service_name, SYSTEM, SCHEMA, evidence, **kwargs)
    except LLMError as error:
        return failed(str(error)), None
    failures = verify.summary_failures(answer.data, answer.sent)
    return (withheld(failures) if failures else note(answer, answer.sent)), answer


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--change-sys-id", help="the change to write to")
    parser.add_argument("--dry-run", action="store_true", help="print the note, write nowhere")
    args = parser.parse_args()

    evidence = notes.load_json(args.evidence)
    run = evidence["run"]
    if run.get("change_type") != "normal":
        print(f"{run.get('change_number')} is a {run.get('change_type')} change: summaries are for normal changes only.")
        return 0
    if not notes.settings(run["service"]).get("change_summary"):
        print(f"services.yaml doesn't ask for change summaries for {run['service']}.")
        return 0
    text, answer = summarize(evidence, run["service"])
    print(text)
    if args.dry_run:
        return 0
    sn = notes.notes_servicenow()
    writers = [("work note as ai.notes", lambda: notes.work_note(sn, args.change_sys_id, text)),
               ("evidence.json attached", lambda: notes.attach(sn, args.change_sys_id, args.evidence))]
    if answer is not None:
        state = {"service": run["service"], "version": run["version"], "jira_key": run.get("jira_key"),
                 "build": {"number": run.get("build"), "url": run.get("build_url")},
                 "change": {"number": run.get("change_number")}}
        writers.append(("tokens and cost to Splunk and Grafana",
                        lambda: notes.send_event(usage_event(answer, TASK, state, withheld=text.startswith("No summary")))))
    return 1 if notes.each(writers) else 0


if __name__ == "__main__":
    sys.exit(main())
