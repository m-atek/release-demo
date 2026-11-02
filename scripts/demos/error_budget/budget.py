"""Video 27's error budget: a release rule that reads production. The first stage
of the release pipeline, "Error budget", counts orders-service production's
requests and failures over a rolling window from CloudWatch, and refuses a
feature when more have failed than the target allows. It runs while
scripts/demos/error_budget/switch.py is on.

    python3 scripts/demos/error_budget/budget.py orders-service --add-stage     the first row, from the first Slack line
    python3 scripts/demos/error_budget/budget.py orders-service                 the stage: exit 0 passes, exit 3 refuses
    python3 scripts/demos/error_budget/budget.py orders-service --no-reading "<reason>"
    python3 scripts/demos/error_budget/budget.py approved                       after Alex's approval, on an override run
    python3 scripts/demos/error_budget/budget.py released                       after Record, on an override run
    python scripts/demos/error_budget/budget.py status [--key ORD-12] [--option standard]   the numbers and the decision, writing nothing
    python scripts/demos/error_budget/budget.py url [--as-of HH:MM] [--live]    the dashboard's address, pinned to that minute's window

The stage, in order:

1. Read, through the evidence reader role: GetMetricData for both metrics in one
   call, 60-second periods, FunctionName alone. Missing minutes count as 0. A
   minute with a seeded count (seed.py, services.yaml's error_budget.seed) reads
   that count and 0 failed, never both: only the quiet day before a take is seeded.
2. Too few requests to judge: one line, pass. Within the budget: the lines, pass.
3. Spent, and override asked: the lines, the change's description and the
   report, override in the run state, pass. The change is Normal because
   override isn't in standard_options.
4. Spent, and this is the budget's own issue: open, a Bug, the label, reported
   by Release Pipeline, the budget's own summary. It passes if at most
   fix_max_changed_lines lines of code changed since production's commit, tests
   and VERSION left out. Over that, or if the commit can't be read: refused, and
   the line says which.
5. Spent, anything else: refused. While requests still fail and no budget issue
   is open, this opens one. The refused issue gets a comment.
6. Every time: .release/reports/error-budget.json and one event to Splunk and
   Grafana Logs, kind=error_budget.

The arithmetic is integers, so it can't round differently from the test: allowed
is requests x (1 - target), rounded down, and it refuses when failed > allowed.
/rollback isn't gated, because going back always lowers risk. It uses no model.
"""

import argparse
import datetime
import json
import sys
import time
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

import yaml  # noqa: E402

import run_state  # noqa: E402
from common import ROOT, get_parameter, people, service, ssm_client, write_json  # noqa: E402

STAGE = "Error budget"
REFUSED = 3
OVERRIDE = "override"
# CloudWatch's delay: the window ends this many minutes ago, on the minute.
DELAY_MINUTES = 3
# Nothing failed for this long: the failures have stopped, and "Back about" may be printed.
STOPPED_MINUTES = 5
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
REPORT = "error-budget.json"
DASHBOARD_UID = "release-demo-error-budget"
DASHBOARD_SLUG = "error-budget"
# Url5xxCount is what AWS documents for "function errors and timeouts", but on this account
# function errors answered 502 and it never appeared: services.yaml names Errors instead.
DEFAULT_METRICS = {"requests": "UrlRequestCount", "failed": "Errors"}


# The arithmetic.

def budget_share(slo):
    """99.9 is 1/1000, exactly."""
    return 1 - Fraction(str(slo)) / 100


def allowed_of(requests, slo):
    """requests x 0.001, rounded down: requests // 1000 at 99.9."""
    return int(requests * budget_share(slo))


def spent_of(failed, requests, slo):
    """failed / (requests x 0.001), whole percent, rounded down: failed * 100000 // requests at 99.9."""
    return int(failed * 100 / (requests * budget_share(slo))) if requests else 0


def as_of_minute(now):
    """The window's end: the whole minute DELAY_MINUTES ago."""
    return int(now // 60) * 60 - DELAY_MINUTES * 60


class Reading:
    """One window: [(minute, requests, failed)], oldest first, every minute present."""

    def __init__(self, series, as_of, slo, hours, seeded=(), under_seed=0):
        self.series, self.as_of, self.slo, self.hours = series, as_of, slo, hours
        # The minutes in this window that are seeded, and the real failures those minutes hide.
        self.seeded, self.under_seed = sorted(seeded), under_seed
        self.requests = sum(requests for _, requests, _ in series)
        self.failed = sum(failed for _, _, failed in series)
        self.allowed = allowed_of(self.requests, slo)
        self.spent = spent_of(self.failed, self.requests, slo)

    @property
    def over(self):
        return self.failed > self.allowed

    @property
    def seeded_until(self):
        """The minute the seeded part ends: requests before it are seeded, from it on measured."""
        return self.seeded[-1] + 60 if self.seeded else None

    def failed_in_last(self, minutes):
        return sum(failed for _, _, failed in self.series[-minutes:])

    @property
    def still_failing(self):
        return self.failed_in_last(STOPPED_MINUTES) > 0

    def burn_rate(self):
        """(failed / requests in the last hour) / 0.001. Shown only: it decides nothing."""
        hour = self.series[-60:]
        requests = sum(row[1] for row in hour)
        failed = sum(row[2] for row in hour)
        return float(Fraction(failed, requests) / budget_share(self.slo)) if requests else 0.0

    def back_at(self):
        """The first minute a release would find the budget back: the failures still in the
        window at or under the allowed number, with requests going on at the last hour's rate,
        and that window's end DELAY_MINUTES behind the clock. A forecast."""
        rate = sum(row[1] for row in self.series[-60:]) // 60
        requests, failed = self.requests, self.failed
        for step, (_, old_requests, old_failed) in enumerate(self.series, start=1):
            requests += rate - old_requests
            failed -= old_failed
            if failed <= allowed_of(requests, self.slo):
                return self.as_of + step * 60 + DELAY_MINUTES * 60
        return None


# The words.

def clock(seconds):
    return datetime.datetime.fromtimestamp(seconds, AMSTERDAM).strftime("%H:%M")


def back_about(reading, now):
    """" Back about 13:47 tomorrow." once the failures have stopped; "" while they go on."""
    if reading.still_failing:
        return ""
    back = reading.back_at()
    if back is None:
        return ""
    same_day = (datetime.datetime.fromtimestamp(back, AMSTERDAM).date()
                == datetime.datetime.fromtimestamp(now, AMSTERDAM).date())
    return f" Back about {clock(back)}{'' if same_day else ' tomorrow'}."


def head(reading, third):
    """The first three lines, always the same but for what follows Spent."""
    return "\n".join([f"Error budget, last {reading.hours} hours to {clock(reading.as_of)}",
                      f"{reading.failed:,} of {reading.requests:,} requests failed. {reading.slo:g}% allows {reading.allowed:,}.",
                      third])


def one_line(reading):
    """The first three lines as one, for the change and the Jira issue."""
    return (f"Error budget, last {reading.hours} hours to {clock(reading.as_of)}: {reading.failed:,} of "
            f"{reading.requests:,} requests failed. {reading.slo:g}% allows {reading.allowed:,}. Spent: {reading.spent}%.")


def source_line(reading, settings):
    """Where the numbers come from, for the override's description, which is on screen."""
    metrics = metrics_of(settings)
    line = f"Source: CloudWatch, {metrics['requests']} and {metrics['failed']}, {settings['function']}."
    if reading.seeded:
        line += f" Requests before {clock(reading.seeded_until)} are seeded, none failed."
    return line


def seeded_line(reading):
    """For status: what of this window is seeded."""
    if not reading.seeded:
        return "Seeded: nothing in this window."
    return (f"Seeded: {len(reading.seeded):,} minutes of this window, to {clock(reading.seeded_until)}, none failed. "
            f"From then on CloudWatch's counts. Real failures the seed hides: {reading.under_seed}.")


def changed_text(count):
    return f"{count} line{'' if count == 1 else 's'} of code changed since production"


def lines_of(count):
    return changed_text(count) + "."


def approver_name():
    """Alex, from people.yaml's approver: true: the first word of the display name."""
    everyone = yaml.safe_load((ROOT / "people.yaml").read_text())
    return next(person["display_name"] for person in everyone.values() if person.get("approver")).split()[0]


def asked_by(state):
    """Who asked, by name: Sam Developer. Never a Slack member id."""
    try:
        for person in people().values():
            if state.get("requested_by") and person.get("slack_user_id") == state["requested_by"]:
                return person["display_name"]
    except (Exception, SystemExit):
        pass
    return "the person who asked"


def ask_line(state, approver):
    return f"To ask {approver}: /release {state['service']} {state['jira_key']} {OVERRIDE}"


def summary_of(service_name, settings):
    return f"Error budget spent: {service_name}, {settings['slo']:g}% over {settings['window_hours']} hours"


# Reading from outside.

def settings_of(service_name):
    return service(service_name)["error_budget"]


def metrics_of(settings):
    return {**DEFAULT_METRICS, **(settings.get("metrics") or {})}


def read_counts(client, function, as_of, hours, metrics, seed=None):
    """{requests, failed and with a seed seeded: {minute: count}} for the window, from one
    GetMetricData call and its pages. Seeded counts read with Maximum, so a second seed.py changes nothing."""
    start = as_of - hours * 3600

    def query(role, namespace, name, stat):
        return {"Id": role, "ReturnData": True,
                "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": name,
                                          "Dimensions": [{"Name": "FunctionName", "Value": function}]},
                               "Period": 60, "Stat": stat}}

    queries = [query(role, "AWS/Lambda", metrics[role], "Sum") for role in ("requests", "failed")]
    if seed:
        queries.append(query("seeded", seed["namespace"], seed["metric"], "Maximum"))
    counts = {entry["Id"]: {} for entry in queries}
    token = None
    while True:
        reply = client.get_metric_data(
            MetricDataQueries=queries, ScanBy="TimestampAscending",
            StartTime=datetime.datetime.fromtimestamp(start, datetime.timezone.utc),
            EndTime=datetime.datetime.fromtimestamp(as_of, datetime.timezone.utc),
            **({"NextToken": token} if token else {}))
        for result in reply["MetricDataResults"]:
            if result.get("StatusCode") not in ("Complete", "PartialData", None):
                raise RuntimeError(f"CloudWatch answered {result['StatusCode']} for {result['Id']}")
            for stamp, value in zip(result["Timestamps"], result["Values"]):
                counts[result["Id"]][int(stamp.timestamp())] = int(value)
        token = reply.get("NextToken")
        if not token:
            return counts


def merged(counts, start, as_of):
    """[(minute, requests, failed)], every minute once: a seeded minute reads its seed and 0 failed,
    never both, and any other minute what AWS/Lambda counted."""
    seeded = counts.get("seeded", {})
    return [(minute, seeded[minute], 0) if minute in seeded
            else (minute, counts["requests"].get(minute, 0), counts["failed"].get(minute, 0))
            for minute in range(start, as_of, 60)]


def read_series(client, function, as_of, hours, metrics, seed=None):
    """[(minute, requests, failed)] for the window."""
    return merged(read_counts(client, function, as_of, hours, metrics, seed), as_of - hours * 3600, as_of)


def take_reading(client, settings, now):
    as_of = as_of_minute(now)
    hours = settings["window_hours"]
    counts = read_counts(client, settings["function"], as_of, hours, metrics_of(settings), settings.get("seed"))
    seeded = counts.get("seeded", {})
    under_seed = sum(count for minute, count in counts["failed"].items() if minute in seeded)
    return Reading(merged(counts, as_of - hours * 3600, as_of), as_of, settings["slo"], hours, seeded, under_seed)


def open_issues(jira, service_name, settings):
    """The budget's own open issues, oldest first. The token is Release Pipeline's, so
    reporter = currentUser() is Release Pipeline. Demo stories have that reporter too (setup.py
    makes them), so the exact summary comes on top: a Story relabelled and retyped still fails."""
    from jira import PROJECT

    jql = (f'project = {PROJECT} AND issuetype = Bug AND labels = "{settings["issue_label"]}" '
           "AND statusCategory != Done AND reporter = currentUser() ORDER BY created")
    summary = summary_of(service_name, settings)
    return [issue["key"] for issue in jira.search(jql, fields="summary") if issue["fields"]["summary"] == summary]


def production_commit(service_name, function):
    """The commit production's /health reports, or None if it can't be read."""
    from common import http_get
    from deploy import lambda_client

    try:
        client = lambda_client(service_name)
        url = client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")
        status, body = http_get(f"{url}/health", timeout=5)
        return json.loads(body).get("commit") if status == 200 else None
    except (Exception, SystemExit) as error:
        print(f"Error budget: production's commit not read ({error.__class__.__name__})")
        return None


def changed_lines(commit, path):
    """Changed lines of code between production's commit and this one: the sum of
    max(added, deleted) per file, tests/ and VERSION left out. None if the commit can't be read."""
    from evidence import git, has_commit

    if not has_commit(commit):
        return None
    result = git("diff", "--numstat", "--no-renames", commit, "HEAD", "--", path)
    if result.returncode != 0:
        return None
    total = 0
    for line in result.stdout.splitlines():
        added, deleted, name = line.split("\t", 2)
        relative = name.removeprefix(path.rstrip("/") + "/")
        if relative == "VERSION" or relative.startswith("tests/"):
            continue
        total += max(int(added) if added != "-" else 1, int(deleted) if deleted != "-" else 1)
    return total


# The decision, with nothing written: the stage and status share it.

def decide(reading, settings, option, key, issues, sized):
    """(result, changed lines). issues() and sized() are only called when the rule needs them.
    The result is too-few, within, override-asked, fix, too-big, no-commit or refused."""
    if reading.requests < settings["min_requests"]:
        return "too-few", None
    if not reading.over:
        return "within", None
    if option == OVERRIDE:
        return OVERRIDE + "-asked", None
    if key in (issues() or []):
        changed = sized()
        if changed is None:
            return "no-commit", None
        return ("fix" if changed <= settings["fix_max_changed_lines"] else "too-big"), changed
    return "refused", None


# Writing it down.

def event_body(state, result, **values):
    return {"build": state["build"].get("number"), "build_url": state["build"].get("url"), "service": state["service"],
            "version": state["version"], "environment": "production", "change": (state.get("change") or {}).get("number"),
            "jira_key": state.get("jira_key"), "stage": STAGE, "event": "error_budget", "result": result, **values}


def send_event(body):
    """To Splunk as events.py sends, and to Grafana Logs as a line of its own kind, as the cost check's."""
    import base64

    from events import send, to_splunk

    ssm = ssm_client()
    url = get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push"
    auth = base64.b64encode(f"{get_parameter(ssm, 'grafana/logs-user')}:{get_parameter(ssm, 'grafana/logs-token')}"
                            .encode()).decode()
    labels = {"source": "release-demo", "service": body["service"], "kind": "error_budget", "environment": "production"}
    loki = send(url, {"streams": [{"stream": labels, "values": [[str(time.time_ns()), json.dumps(body)]]}]},
                {"Authorization": f"Basic {auth}"})
    print(f"event error_budget {body['result']}: Splunk {to_splunk(ssm, body)}, Grafana {loki}")


def report_path():
    return run_state.REPORTS / REPORT


def numbers_of(reading):
    return {"requests": reading.requests, "failed": reading.failed, "allowed": reading.allowed, "spent": reading.spent,
            "slo": reading.slo, "window_hours": reading.hours, "as_of": run_state.iso(reading.as_of),
            "seeded_minutes": len(reading.seeded),
            "seeded_until": run_state.iso(reading.seeded_until) if reading.seeded else None}


def record(state, settings, result, blocks, reading=None, reason=None, **extra):
    """The report, the run state, Slack and the event. Slack and the event only print on an error:
    what the stage decided stands either way."""
    from notify import post

    numbers = numbers_of(reading) if reading else {}
    write_json(report_path(), {
        "service": state["service"], "version": state["version"], "build": state["build"].get("number"),
        "jira_key": state.get("jira_key"), "option": state.get("option"), "result": result, "reason": reason, **numbers,
        "function": settings["function"], "metrics": metrics_of(settings),
        "burn_rate_last_hour": round(reading.burn_rate(), 2) if reading else None,
        "failed_last_minutes": {str(STOPPED_MINUTES): reading.failed_in_last(STOPPED_MINUTES)} if reading else None,
        "real_failed_under_the_seed": reading.under_seed if reading else None,
        **extra,
        # The per-minute series: as ISO minutes, so the report reads without a converter.
        "series": [[run_state.iso(minute), requests, failed] for minute, requests, failed in reading.series] if reading else [],
    })
    state["error_budget"] = {"result": result, **numbers}
    state.setdefault("lines", []).extend(blocks)
    run_state.save(state)
    for block in blocks:
        print(block)
    body = event_body(state, result, reason=reason, **numbers, **{k: v for k, v in extra.items() if k == "changed_lines"})
    for name, write in (("Slack", lambda: post(state)), ("events", lambda: send_event(body))):
        try:
            write()
        except BaseException as error:  # fail() exits: still, nothing here undoes the decision
            print(f"Error budget: {name} not written ({error.__class__.__name__}: {str(error)[:200]})")


def no_reading(state, settings, reason):
    """Toward a person, never silent: the release carries on to a Normal change."""
    line = f"Error budget: no reading ({reason}). {approver_name()} decides."
    state["force_normal"] = True
    state.setdefault("change_details", []).append(line)
    record(state, settings, "no-reading", [line], reason=reason)
    return 0


def comment_on(jira, key, text):
    from jira import paragraph

    try:
        jira.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": paragraph(text)})
    except (Exception, SystemExit) as error:
        print(f"Error budget: no comment on {key} ({error.__class__.__name__})")


def dashboard_address():
    """For the issue's description, which goes without it if Grafana's address can't be read."""
    try:
        return get_parameter(ssm_client(), "grafana/stack-url").rstrip("/") + f"/d/{DASHBOARD_UID}"
    except (Exception, SystemExit) as error:
        print(f"Error budget: the dashboard's address not read ({error.__class__.__name__})")
        return "the dashboard Error budget, folder Release demo"


def fix_line(state, jira, reading, settings, already_open):
    """The line that names the budget's issue, opening one if none is open. Only while requests
    still fail: once they have stopped, the refusal names no issue, so it opens none."""
    if already_open:
        return f"The fix goes under {already_open[0]}."
    try:
        description = f"{one_line(reading)}\nDashboard: {dashboard_address()}"
        key = jira.create("Bug", summary_of(state["service"], settings), description, labels=[settings["issue_label"]])
    except (Exception, SystemExit) as error:
        print(f"Error budget: no issue opened ({error.__class__.__name__})")
        return "No issue opened: Jira didn't answer."
    return f"The fix goes under {key}, opened now."


def run_stage(state, service_name, session=None, jira=None, now=None):
    settings = settings_of(service_name)
    now = now or run_state.now()
    approver = approver_name()
    try:
        if session is None:
            from evidence import reader_session

            session = reader_session()
    except BaseException as error:
        print(f"Error budget: the evidence reader role isn't available ({error.__class__.__name__})")
        return no_reading(state, settings, "the evidence reader role isn't available")
    try:
        reading = take_reading(session.client("cloudwatch"), settings, now)
    except Exception as error:
        print(f"Error budget: CloudWatch not read ({error.__class__.__name__}: {str(error)[:200]})")
        return no_reading(state, settings, "CloudWatch didn't answer")

    key, option = state.get("jira_key"), state.get("option")
    found = {"client": jira, "issues": None, "jira_down": False, "commit": None}

    def issues():
        if found["issues"] is None:
            try:
                if found["client"] is None:
                    from jira import Jira

                    found["client"] = Jira()
                found["issues"] = open_issues(found["client"], service_name, settings)
            except (Exception, SystemExit) as error:
                print(f"Error budget: Jira not read ({error.__class__.__name__})")
                found["issues"], found["jira_down"] = [], True
        return found["issues"]

    def sized():
        found["commit"] = production_commit(service_name, settings["function"])
        return changed_lines(found["commit"], service(service_name)["path"]) if found["commit"] else None

    result, changed = decide(reading, settings, option, key, issues, sized)
    spent_line = f"Spent: {reading.spent}%."
    ask = ask_line(state, approver)

    if result == "too-few":
        line = f"Error budget: {reading.requests:,} requests in the last {reading.hours} hours, too few to judge."
        record(state, settings, result, [line], reading)
        return 0
    if result == "within":
        record(state, settings, result, [head(reading, spent_line)], reading)
        return 0
    if result == "override-asked":
        back = back_about(reading, now)
        state["override"] = True
        state.setdefault("change_details", []).extend([
            one_line(reading),
            source_line(reading, settings),
            f"Override asked by {asked_by(state)}. While the budget is spent, only the budget's own issue releases "
            "without this approval."])
        state.setdefault("change_attachments", []).append(str(report_path()))
        record(state, settings, result, [head(reading, spent_line + back), f"Override asked: {approver} decides in ServiceNow."],
               reading)
        return 0
    if result == "fix":
        commit, head_commit = found["commit"], state.get("commit") or "HEAD"
        state.setdefault("change_work_notes", []).extend([
            one_line(reading),
            f"Passed as this budget's own issue, {key}. {changed_text(changed)}, {commit[:7]}..{head_commit[:7]}."])
        record(state, settings, result, [head(reading, spent_line), f"{key} passes: this budget's own issue,\n{lines_of(changed)}"],
               reading, budget_issue=key, changed_lines=changed, production_commit=commit, head_commit=head_commit)
        return 0

    # Refused, one way or another: exit 3, errorBudget.groovy raises, and stopped() says where it stopped.
    refused = f"{spent_line} Refused."
    if result == "too-big":
        blocks = [head(reading, refused),
                  f"{lines_of(changed)}\nA fix may change {settings['fix_max_changed_lines']}.\n{ask}"]
        reason = "too big"
    elif result == "no-commit":
        blocks = [head(reading, refused), f"Production's commit can't be read: no line count.\n{ask}"]
        reason = "production's commit not read"
    elif reading.still_failing:
        named = found["client"] and not found["jira_down"]
        line = fix_line(state, found["client"], reading, settings, issues()) if named else "No issue opened: Jira didn't answer."
        blocks = [head(reading, refused), f"{line}\n{ask}"]
        reason = None
    else:
        blocks = [head(reading, refused + back_about(reading, now)), ask]
        reason = None
    if found["client"]:
        comment_on(found["client"], key, f"Release of {state['service']} {state['version']} refused: the error budget is "
                                         f"spent. {one_line(reading)} Build: {state['build'].get('url', '')}")
    # The event's result is one of the spec's six; the report and the reason say which refusal.
    record(state, settings, "refused", blocks, reading, reason=reason, changed_lines=changed,
           production_commit=found["commit"])
    return REFUSED


def add_stage(state):
    """The first row, from the first Slack line."""
    if STAGE not in state["stages"]:
        state["stages"] = {STAGE: {"status": "waiting"}, **state["stages"]}
        run_state.save(state)


def approved(state):
    """After the approved line, on an override run only: the approver's comment goes on the record."""
    if not state.get("override"):
        return 0
    import change as change_module
    from notify import post

    settings = settings_of(state["service"])
    name = (state.get("change") or {}).get("approved_by") or approver_name()
    try:
        sn, _ = change_module.connect()
        comment = change_module.approved_comment(sn, state["change"])
    except (Exception, SystemExit) as error:
        print(f"Error budget: the approval's comment not read ({error.__class__.__name__})")
        comment = ""
    spent = (state.get("error_budget") or {}).get("spent", "?")
    if not comment and settings.get("reason_required", True):
        state.setdefault("lines", []).append(f"Override refused: no reason in {name}'s approval.")
        run_state.save(state)
        _quietly("Slack", lambda: post(state))
        return REFUSED
    if comment:
        limit = change_module.REJECTION_COMMENT_LIMIT
        shown = comment if len(comment) <= limit else comment[:limit - 3].rstrip() + "..."
        state.setdefault("lines", []).append(f"Override reason: {shown}")
    state["close_notes_extra"] = (f"Released with the error budget {spent}% spent. "
                                  f"Override approved by {name}{': ' + comment if comment else '.'}")
    run_state.save(state)
    _quietly("Slack", lambda: post(state))
    numbers = {key: value for key, value in (state.get("error_budget") or {}).items() if key != "result"}
    _quietly("events", lambda: send_event(event_body(state, "override-approved", approver=name, comment=comment, **numbers)))
    return 0


def released(state):
    """After Record, on an override run only: a mark of its own. The release's green mark comes from Record as always."""
    if not state.get("override"):
        return 0
    from events import annotate

    name = (state.get("change") or {}).get("approved_by") or approver_name()
    text = (f"{state['service']} {state['version']} released with the error budget spent, "
            f"override by {name} ({state['jira_key']})")
    _quietly("Grafana", lambda: print(f"Grafana annotation: {annotate(ssm_client(), text, ['error-budget', OVERRIDE, state['service']])}"))
    return 0


def _quietly(name, write):
    try:
        write()
    except BaseException as error:  # the release's next step doesn't depend on this one
        print(f"Error budget: {name} not written ({error.__class__.__name__}: {str(error)[:200]})")


# Status and the dashboard's address, for the take.

def status_session():
    """On the tools host the evidence reader role; from the Mac your own sign-in, which that role doesn't trust."""
    import host

    from common import aws_session

    if host.on_host():
        from evidence import reader_session

        return reader_session()
    return aws_session()


def status(service_name, key, option, out=print, session=None, jira=None, now=None):
    settings = settings_of(service_name)
    now = now or time.time()
    reading = take_reading((session or status_session()).client("cloudwatch"), settings, now)
    out(head(reading, f"Spent: {reading.spent}%.{back_about(reading, now)}"))
    out(f"Failed in the last {STOPPED_MINUTES} minutes: {reading.failed_in_last(STOPPED_MINUTES)}. "
        f"Burn rate, last hour: {reading.burn_rate():.1f}x.")
    if settings.get("seed"):
        out(seeded_line(reading))

    def issues():
        try:
            client = jira
            if client is None:
                from jira import Jira

                client = Jira()
            found = open_issues(client, service_name, settings)
        except (Exception, SystemExit) as error:
            out(f"Jira not read ({error.__class__.__name__}), so no budget issue counts.")
            return []
        out(f"Open budget issues: {', '.join(found) or 'none'}.")
        return found

    def sized():
        commit = production_commit(service_name, settings["function"])
        return changed_lines(commit, service(service_name)["path"]) if commit else None

    result, changed = decide(reading, settings, option, key, issues, sized)
    asked = f"{key or 'another key'}{f', option {option}' if option else ''}"
    out(f"Decision for {asked}: {result}" + (f" ({lines_of(changed)})" if changed is not None else "") + ".")
    return 0


def dashboard_url(stack, hours, as_of=None):
    """The address pinned to the window ending at as_of. It stops a second short of that minute:
    Grafana may round a CloudWatch end time up to the next minute, and then asks for the same window either way."""
    base = f"{stack.rstrip('/')}/d/{DASHBOARD_UID}/{DASHBOARD_SLUG}?orgId=1"
    if as_of is None:
        return base
    return f"{base}&from={(as_of - hours * 3600) * 1000}&to={as_of * 1000 - 1000}"


def pinned_minute(text, now):
    """HH:MM in Amsterdam as a minute: today's, or yesterday's if that is still to come."""
    hour, minute = (int(part) for part in text.split(":"))
    today = datetime.datetime.fromtimestamp(now, AMSTERDAM).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if today.timestamp() > now:
        today -= datetime.timedelta(days=1)
    return int(today.timestamp())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command in ("approved", "released"):
        return {"approved": approved, "released": released}[command](run_state.load())
    if command == "status":
        parser = argparse.ArgumentParser(description="The numbers and the decision, writing nothing")
        parser.add_argument("--service", default="orders-service")
        parser.add_argument("--key", help="the Jira key it would decide for")
        parser.add_argument("--option", default="")
        args = parser.parse_args(argv[1:])
        return status(args.service, args.key or run_state.load().get("jira_key"), args.option)
    if command == "url":
        parser = argparse.ArgumentParser(description="The dashboard's address")
        parser.add_argument("--service", default="orders-service")
        parser.add_argument("--as-of", metavar="HH:MM", help="the minute in Slack's line; default the newest whole window")
        parser.add_argument("--live", action="store_true", help="not pinned: the last 24 hours, and new marks show")
        args = parser.parse_args(argv[1:])
        now = time.time()
        as_of = None if args.live else (pinned_minute(args.as_of, now) if args.as_of else as_of_minute(now))
        print(dashboard_url(get_parameter(ssm_client(), "grafana/stack-url"), settings_of(args.service)["window_hours"], as_of))
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--add-stage", action="store_true", help="put the stage first in this run's stage list")
    parser.add_argument("--no-reading", metavar="REASON", help="record that there is no reading, and why")
    args = parser.parse_args(argv)
    state = run_state.load()
    if args.add_stage:
        add_stage(state)
        return 0
    if args.no_reading:
        return no_reading(state, settings_of(args.service), args.no_reading)
    return run_stage(state, args.service)


if __name__ == "__main__":
    sys.exit(main())
