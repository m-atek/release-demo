"""Records as ServiceNow, CloudTrail, Lambda and JFrog give them, for the
auditor sample's tests: one change of each kind the report must answer.
Every value is made up here, as the real ones would look."""

import base64
import datetime
import hashlib
import json

UTC = datetime.timezone.utc
SN_URL = "https://servicenow.example"
SAM, ALEX = "sam-sys-id", "alex-sys-id"
ROUTINE, ROLLBACK = "routine-version-id", "rollback-version-id"


def field(value, display=None):
    return {"value": value, "display_value": value if display is None else display}


def stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def when(day, hour, minute, second=0):
    return datetime.datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def package_sha(number):
    return hashlib.sha256(f"package of {number}".encode()).hexdigest()


def change(number, short, created, start, end, state="3", requested=SAM, template=None, close="successful"):
    return {"sys_id": field(f"{number}-id"), "number": field(number), "state": field(state),
            "type": field("standard" if template else "normal"), "short_description": field(short),
            "requested_by": field(requested, "Sam Developer" if requested == SAM else "Alex Change Manager"),
            "approval": field("approved"), "work_start": field(stamp(start) if start else ""),
            "work_end": field(stamp(end) if end else ""), "sys_created_on": field(stamp(created)),
            "close_code": field(close, close.capitalize()), "close_notes": field(""),
            "std_change_producer_version": field(template or "", ""), "cmdb_ci": field("orders-item")}


# (number, short description, created, work start, work end, state, template, close code), and its moves.
CHANGES = [
    ("CHG0030030", "Release orders-service 1.2.3 (ORD-4)", when(27, 9, 0), when(27, 9, 4), when(27, 9, 6), "3", ROUTINE,
     "successful"),
    ("CHG0030034", "Release orders-service 1.2.4 (ORD-5)", when(27, 10, 0), when(27, 10, 4), when(27, 10, 7), "3",
     ROUTINE, "unsuccessful"),
    ("CHG0030041", "Release orders-service 1.4.0 (ORD-12)", when(28, 13, 0), when(28, 13, 8), when(28, 13, 10), "3",
     None, "successful"),
    ("CHG0030045", "Release orders-service 1.4.0 (ORD-12)", when(28, 15, 0), when(28, 15, 8), when(28, 15, 10), "3",
     None, "successful"),
    ("CHG0030046", "Release orders-service 1.4.0 (ORD-12)", when(28, 16, 0), when(28, 16, 8), when(28, 16, 10), "3",
     None, "successful"),
    ("CHG0030047", "Release orders-service 1.4.0 (ORD-12)", when(28, 17, 0), when(28, 17, 8), when(28, 17, 10), "3",
     None, "successful"),
    ("CHG0030048", "Release orders-service 1.4.1 (ORD-13)", when(29, 9, 0), None, None, "4", None, "successful"),
    ("CHG0030049", "Release orders-service 1.4.1 (ORD-13)", when(29, 10, 0), when(29, 10, 5), when(29, 10, 6), "3",
     ROUTINE, "unsuccessful"),
    ("CHG0030050", "Rollback of orders-service in production: 1.4.1 to 1.4.0", when(29, 11, 0, 30), when(29, 11, 0),
     when(29, 11, 1), "3", ROLLBACK, "successful"),
    ("CHG0030051", "Release orders-service 1.4.0 (ORD-12)", when(29, 12, 0), None, None, "-2", None, "successful"),
]
# Lambda versions and when production's live alias moved to them.
MOVES = [
    ("CHG0030030", when(27, 9, 5), "20", "1.2.3"),
    ("CHG0030034", when(27, 10, 5), "21", "1.2.4"),
    ("CHG0030034", when(27, 10, 6), "20", "1.2.3"),  # the automatic rollback
    ("CHG0030041", when(28, 13, 9), "30", "1.4.0"),
    ("CHG0030045", when(28, 15, 9), "31", "1.4.0"),
    ("CHG0030046", when(28, 16, 9), "32", "1.4.0"),
    ("CHG0030047", when(28, 17, 9), "33", "1.4.0"),
    ("CHG0030050", when(29, 11, 0, 10), "30", "1.4.0"),
    (None, when(29, 20, 0), "34", "1.3.0"),  # reset.py's housekeeping, in no change's window
]


def changes():
    return [change(n, s, c, ws, we, state, template=t, close=cc) for n, s, c, ws, we, state, t, cc in CHANGES]


def approvals():
    rows = []
    for number, approver, updated in [("CHG0030041", ALEX, when(28, 13, 5)), ("CHG0030045", ALEX, when(28, 15, 30)),
                                      ("CHG0030046", SAM, when(28, 16, 5)), ("CHG0030047", ALEX, when(28, 17, 5))]:
        rows.append({"sys_id": field(f"approval-{number}"), "sysapproval": field(f"{number}-id"),
                     "approver": field(approver, "Alex Change Manager" if approver == ALEX else "Sam Developer"),
                     "state": field("approved"), "sys_created_on": field(stamp(updated - datetime.timedelta(minutes=3))),
                     "sys_updated_on": field(stamp(updated))})
    return rows


def junit(failures=0):
    return (f'<testsuites><testsuite name="pytest" tests="14" failures="{failures}" errors="0" skipped="0">'
            '</testsuite></testsuites>').encode()


def attachments_for(number, version, build):
    info = {"service": "orders-service", "version": version, "commit": "c0ffee", "package": f"orders-service-{version}.zip",
            "sha256": package_sha(number), "jira_key": "ORD-12", "build": build,
            "build_url": f"https://jenkins-demo.es.nl/job/release/{build}/"}
    return {"junit.xml": junit(), "sbom.cdx.json": json.dumps({"components": [{"name": "boto3"}] * 7}).encode(),
            "grype.json": json.dumps({"matches": [{"vulnerability": {"severity": "Medium"}}]}).encode(),
            "build-info.json": json.dumps(info).encode()}


ATTACHED = {"CHG0030030": ("1.2.3", "40"), "CHG0030034": ("1.2.4", "41"), "CHG0030041": ("1.4.0", "57"),
            "CHG0030045": ("1.4.0", "58"), "CHG0030046": ("1.4.0", "59"), "CHG0030047": ("1.4.0", "60"),
            "CHG0030049": ("1.4.1", "61")}


class FakeSN:
    url = SN_URL
    auth = "Basic test"

    def __init__(self):
        self.files = {}
        self.attachments = []
        for number, (version, build) in ATTACHED.items():
            for name, data in attachments_for(number, version, build).items():
                sys_id = f"att-{number}-{name}"
                self.files[sys_id] = data
                self.attachments.append({"sys_id": field(sys_id), "table_sys_id": field(f"{number}-id"),
                                         "file_name": field(name), "size_bytes": field(str(len(data))),
                                         "sys_created_on": field("2026-10-28 13:01:00")})
        self.queries = []

    def call(self, method, path, body=None, **query):
        self.queries.append((method, path, query.get("sysparm_query")))
        assert method == "GET", "the evidence reader only reads"
        table = path.rsplit("/", 1)[-1]
        q = query.get("sysparm_query", "")
        if table == "cmdb_ci_appl":
            return [{"sys_id": field("orders-item"), "name": field("orders-service")}]
        if table == "change_request":
            return changes()
        if table == "sysapproval_approver":
            ids = q.removeprefix("sysapprovalIN").split(",")
            return [a for a in approvals() if a["sysapproval"]["value"] in ids]
        if table == "std_change_producer_version":
            return [{"sys_id": field(ROUTINE), "std_change_producer": field("routine-id")},
                    {"sys_id": field(ROLLBACK), "std_change_producer": field("rollback-id")}]
        if table == "std_change_record_producer":
            return [{"sys_id": field("routine-id"), "name": field("Routine release of orders-service"), "active": field("true")},
                    {"sys_id": field("rollback-id"), "name": field("Rollback of orders-service"), "active": field("true")}]
        if table == "sys_attachment":
            ids = q.split("table_sys_idIN")[1].split(",")
            return [a for a in self.attachments if a["table_sys_id"]["value"] in ids]
        raise AssertionError(f"unexpected table {table}")


class FakeReaderDownloads:
    """Stands in for ServiceNowReader.download, which reads bytes over HTTP."""

    def __init__(self, sn):
        self.sn = sn

    def __call__(self, sys_id):
        return self.sn.files[sys_id]


class FakeCloudTrail:
    def __init__(self):
        self.calls = 0

    def lookup_events(self, **args):
        self.calls += 1
        events = []
        for i, (number, moment, version, _) in enumerate(MOVES):
            detail = {"eventName": "UpdateAlias20150331", "sourceIPAddress": "198.51.100.7",
                      "userIdentity": {"arn": "arn:aws:sts::123456789012:assumed-role/release-demo-deploy-orders-service/x",
                                       "accessKeyId": "ASIA" + "EXAMPLE0000000001"[:16]},
                      "requestParameters": {"functionName": "orders-service-production", "name": "live",
                                            "functionVersion": version}}
            events.append({"EventId": f"event-{i}", "EventTime": moment, "CloudTrailEvent": json.dumps(detail)})
        staging = {"requestParameters": {"functionName": "orders-service-staging", "name": "live", "functionVersion": "9"}}
        events.append({"EventId": "staging", "EventTime": when(28, 13, 3), "CloudTrailEvent": json.dumps(staging)})
        half = len(events) // 2
        if "NextToken" not in args:
            return {"Events": events[:half], "NextToken": "page-2"}
        return {"Events": events[half:]}


class FakeLambda:
    def __init__(self, wrong=()):
        self.wrong = set(wrong)

    def get_function_configuration(self, FunctionName, Qualifier):
        number = next((n for n, _, v, _ in MOVES if v == Qualifier and n), None)
        app = next(a for _, _, v, a in MOVES if v == Qualifier)
        digest = bytes.fromhex(package_sha(number)) if number and number not in self.wrong else b"\0" * 32
        return {"FunctionName": FunctionName, "Version": Qualifier, "Description": app,
                "CodeSha256": base64.b64encode(digest).decode(),
                "Environment": {"Variables": {"DELIVERY_DAYS": "2", "ENVIRONMENT": "production"}},
                "ResponseMetadata": {"RequestId": "x"}}


class FakeJFrog:
    def __init__(self, wrong=()):
        self.wrong = set(wrong)
        self.read = {}

    def stored(self, path):
        build = path.split("/")[2]
        number = next(n for n, (_, b) in ATTACHED.items() if b == build)
        sha = "f" * 64 if number in self.wrong else package_sha(number)
        record = {"path": path, "checksums": {"sha256": sha}}
        self.read[f"jfrog/{path.replace('/', '_')}.json"] = record
        return record
