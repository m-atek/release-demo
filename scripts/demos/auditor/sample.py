"""The auditor's sample: the population, the draw, and the five answers.

Pure functions over the records records.py read, so the tests can give
each answer its own fixture: yes, no, pre-approved, by design and
"no: can't tell from the record".

- Population: orders-service changes created in the period, state Closed,
  with at least one move of production's live alias inside the change's
  implement window (work_start to work_end, two minutes either side).
  Cancelled changes, changes not closed and closed ones without a production
  move are left out, and counted.
- Draw: each change ranked by SHA-256 of "<seed>:<change number>", the
  lowest [size] taken, then listed by number. No random module.
- Answers, from the records only. A change made from an active standard
  change template is "pre-approved", a /rollback change is recorded after
  its move on purpose ("by design"), and nothing is hidden.
"""

import datetime
import json
import re
import tempfile
from pathlib import Path

import evidence
from records import display, utc, value

WINDOW = datetime.timedelta(minutes=2)
CLOSED, CANCELLED = "3", "4"
YES, NO, PRE_APPROVED, BY_DESIGN = "yes", "no", "pre-approved", "by design"
CANT_TELL = "no: can't tell from the record"
MEETS = {YES, PRE_APPROVED, BY_DESIGN}
QUESTIONS = ["1. Approver is not the requester", "2. Approved before the production deploy",
             "3. Deployed code matches the approved package (SHA-256)", "4. Tests attached, all passed",
             "5. SBOM and scan attached"]
RELEASE = re.compile(r"^Release (?P<service>\S+) (?P<version>\d+\.\d+\.\d+) \((?P<key>[A-Z]+-\d+)\)")
ROLLBACK = re.compile(r"^Rollback of (?P<service>\S+) in production: (?P<was>\S+) to (?P<back>\S+)")


def window_moves(change, moves):
    start, end = utc(value(change, "work_start")), utc(value(change, "work_end"))
    if not start or not end:
        return []
    return [m for m in moves if start - WINDOW <= m["time"] <= end + WINDOW]


def population(changes, moves):
    """(included: [(change, its moves)], left out: {why: count})."""
    included, left_out = [], {"cancelled": 0, "not closed": 0, "closed without a production move": 0}
    for change in changes:
        state = str(value(change, "state"))
        if state == CANCELLED:
            left_out["cancelled"] += 1
        elif state != CLOSED:
            left_out["not closed"] += 1
        elif not window_moves(change, moves):
            left_out["closed without a production move"] += 1
        else:
            included.append((change, window_moves(change, moves)))
    return included, left_out


def draw(included, size, seed):
    """The sample: the lowest SHA-256 of "<seed>:<number>", listed by number."""
    by_number = {value(change, "number"): (change, moves) for change, moves in included}
    chosen = evidence.ranked(seed, list(by_number))[:size]
    return [by_number[number] for number in sorted(chosen)]


class Answer:
    def __init__(self, text, lines=(), links=()):
        self.text, self.lines, self.links = text, list(lines), list(links)

    @property
    def meets(self):
        return self.text in MEETS


def stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if moment else "unknown"


def kind(change, template, templates_named):
    """rollback, standard (from an active template) or normal."""
    short = value(change, "short_description") or ""
    if (template and template["name"] == templates_named["rollback"]) or ROLLBACK.match(short):
        return "rollback"
    if template and template["active"]:
        return "standard"
    return "normal"


def answer_approver(change, kind_, template, approvals):
    if kind_ in ("standard", "rollback"):
        name = template["name"] if template else "a standard change template"
        return Answer(PRE_APPROVED, [f"Pre-approved: made from the standard change template \"{name}\"."])
    approved = [a for a in approvals if value(a, "state") == "approved"]
    requester = value(change, "requested_by")
    if not approved:
        return Answer(NO, ["No approved approval record."])
    if any(value(a, "approver") == requester for a in approved):
        return Answer(NO, [f"Approved by the requester, {display(change, 'requested_by')}."])
    return Answer(YES, [f"Requested by {display(change, 'requested_by')}; approved by "
                        f"{', '.join(display(a, 'approver') for a in approved)}."])


def answer_before(change, kind_, template, approvals, deploy):
    moved = (f"Production live alias moved to version {deploy['version']}: {stamp(deploy['time'])} "
             "(CloudTrail, UpdateAlias)")
    if kind_ == "rollback":
        return Answer(BY_DESIGN, ["A /rollback is recorded after production is back, on purpose: the recovery "
                                  "never waits for ServiceNow.", moved])
    if kind_ == "standard":
        created = utc(value(change, "sys_created_on"))
        name = template["name"] if template else "a standard change template"
        line = f"Pre-approved by the template \"{name}\"; the change was opened {stamp(created)}"
        return Answer(PRE_APPROVED if created <= deploy["time"] else CANT_TELL, [line, moved])
    approved = [a for a in approvals if value(a, "state") == "approved"]
    if not approved:
        return Answer(NO, ["No approved approval record.", moved])
    last = max(approved, key=lambda a: utc(value(a, "sys_updated_on")))
    when = utc(value(last, "sys_updated_on"))
    line = f"Approved by {display(last, 'approver')}: {stamp(when)} (approval record)"
    if when < deploy["time"]:
        return Answer(YES, [line, moved])
    return Answer(CANT_TELL, [line, moved, "The approval record was last updated after the deploy, so its time "
                                           "can't show the approval came first."])


def answer_package(info, jfrog_sha, lambda_sha, path, deploy, function):
    """Question 3: the three SHA-256 values, side by side."""
    lines = [f"In the change (build-info.json): {info.get('sha256') if info else 'no build-info.json attached'}",
             f"In JFrog ({path or 'no path'}): {jfrog_sha or 'not stored'}",
             f"In AWS ({function} version {deploy['version']}, CodeSha256): {lambda_sha or 'not read'}"]
    values = {info.get("sha256") if info else None, jfrog_sha, lambda_sha}
    return Answer(YES if len(values) == 1 and None not in values else NO, lines)


def read_junit(data):
    with tempfile.NamedTemporaryFile(suffix=".xml") as file:
        Path(file.name).write_bytes(data)
        return evidence.junit_counts(file.name)


def read_json_file(data, reader):
    with tempfile.NamedTemporaryFile(suffix=".json") as file:
        Path(file.name).write_bytes(data)
        return reader(file.name)


def answer_tests(files):
    if "junit.xml" not in files:
        return Answer(NO, ["No junit.xml attached."])
    counts = read_junit(files["junit.xml"])
    line = (f"junit.xml: {counts['total']} tests, {counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['errors']} errors, {counts['skipped']} skipped")
    ok = counts["total"] > 0 and counts["failed"] == 0 and counts["errors"] == 0
    return Answer(YES if ok else NO, [line])


def sbom_subject(data):
    """An SBOM's format and the name of what it describes (CycloneDX's metadata.component)."""
    try:
        bom = json.loads(data)
    except ValueError:
        return None, None
    if not isinstance(bom, dict):
        return None, None
    return bom.get("bomFormat"), ((bom.get("metadata") or {}).get("component") or {}).get("name")


def answer_scan(files, service="orders-service"):
    """Both attached, the SBOM a CycloneDX SBOM of this service, the scan without a critical finding.
    A service without third-party packages has a true SBOM with 0 components, as orders-service's
    are: so the count is shown, and the subject is what is checked."""
    missing = [name for name in ("sbom.cdx.json", "grype.json") if name not in files]
    if missing:
        return Answer(NO, [f"Not attached: {', '.join(missing)}."])
    form, subject = sbom_subject(files["sbom.cdx.json"])
    of_service = form == "CycloneDX" and subject == service
    components = read_json_file(files["sbom.cdx.json"], evidence.sbom_components) if of_service else 0
    counts = read_json_file(files["grype.json"], evidence.grype_counts)
    lines = [f"sbom.cdx.json: a CycloneDX SBOM of {subject}, {components} components" if of_service else
             f"sbom.cdx.json: not a CycloneDX SBOM of {service} ({form or 'no format'}, of {subject or 'nothing named'})",
             f"grype.json: critical {counts['critical']}, high {counts['high']}, medium {counts['medium']}, "
             f"low {counts['low']}"]
    return Answer(YES if of_service and counts["critical"] == 0 else NO, lines)


def result(change, kind_, moves, versions):
    """Live, or Rolled back to <version>, from the second alias move and the close code."""
    if kind_ == "rollback":
        back = ROLLBACK.match(value(change, "short_description") or "")
        return f"Rollback live: {back.group('back')}" if back else "Rollback live"
    if len(moves) > 1 or str(value(change, "close_code")).lower() == "unsuccessful":
        last = moves[-1]["version"] if len(moves) > 1 else None
        return f"Rolled back to {versions.get(last, last) or 'the previous version'}"
    return "Live"


def findings(rows):
    """The answers only, sorted, with no run times: what the findings hash covers."""
    return sorted(({"change": row["number"], "version": row["version"],
                    "answers": [answer.text for answer in row["answers"]], "result": row["result"]} for row in rows),
                  key=lambda item: item["change"])
