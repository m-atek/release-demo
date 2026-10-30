"""The original records the auditor's sample reads, through read-only identities:

- ServiceNow as evidence.reader (web service access only, snc_read_only):
  orders-service's changes, their approval records, their attachments, and
  the standard change templates they came from;
- CloudTrail's event history and orders-service-production's Lambda
  versions through the role release-demo-evidence-reader, which only the
  tools host may assume (from the Mac, your own session reads them);
- JFrog's stored SHA-256 as evidence-reader, read on candidates and
  releases, no deploy.

Each reader keeps what it read, as read, for the evidence ZIP.
"""

import concurrent.futures
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

EVIDENCE_ROLE = "release-demo-evidence-reader"
UPDATE_ALIAS = "UpdateAlias20150331"
CLOUDTRAIL_PAUSE = 0.5  # LookupEvents allows two calls a second
CHANGE_FIELDS = ("sys_id,number,state,type,short_description,requested_by,approval,work_start,work_end,sys_created_on,"
                 "close_code,close_notes,std_change_producer_version,cmdb_ci")


def utc(text):
    """ServiceNow's stored times are UTC, "2026-10-29 14:02:16"."""
    if not text:
        return None
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)


def value(record, field):
    item = record.get(field)
    return item.get("value") if isinstance(item, dict) else item


def display(record, field):
    item = record.get(field)
    return item.get("display_value") if isinstance(item, dict) else item


class ServiceNowReader:
    def __init__(self, sn):
        self.sn = sn
        self.read = {}  # name in the ZIP: what ServiceNow returned

    def query(self, table, query, fields, keep_as=None, **extra):
        rows = self.sn.call("GET", f"/api/now/table/{table}", sysparm_query=query, sysparm_fields=fields,
                            sysparm_display_value="all", sysparm_exclude_reference_link="true",
                            sysparm_limit=extra.pop("limit", 1000), **extra) or []
        if keep_as:
            self.read[keep_as] = rows
        return rows

    def item(self, name):
        rows = self.query("cmdb_ci_appl", f"name={name}", "sys_id,name", limit=1)
        return value(rows[0], "sys_id") if rows else None

    def changes(self, item, start, end):
        """The item's changes created between start and end (UTC), with a day either side; filtered after."""
        wide = f"sys_created_on>={(start - datetime.timedelta(days=1)):%Y-%m-%d %H:%M:%S}^" \
               f"sys_created_on<={(end + datetime.timedelta(days=1)):%Y-%m-%d %H:%M:%S}"
        rows = self.query("change_request", f"cmdb_ci={item}^{wide}^ORDERBYnumber", CHANGE_FIELDS,
                          keep_as="servicenow/changes.json")
        return [row for row in rows if start <= utc(value(row, "sys_created_on")) < end]

    def approvals(self, change_ids):
        if not change_ids:
            return []
        return self.query("sysapproval_approver", f"sysapprovalIN{','.join(change_ids)}",
                          "sys_id,sysapproval,approver,state,sys_created_on,sys_updated_on",
                          keep_as="servicenow/approvals.json")

    def templates(self, version_ids):
        """{producer version sys_id: {"sys_id", "name", "active"}} of the standard change templates."""
        if not version_ids:
            return {}
        versions = self.query("std_change_producer_version", f"sys_idIN{','.join(version_ids)}",
                              "sys_id,std_change_producer", keep_as="servicenow/template-versions.json")
        producers = {value(v, "std_change_producer") for v in versions}
        rows = self.query("std_change_record_producer", f"sys_idIN{','.join(sorted(p for p in producers if p))}",
                          "sys_id,name,active", keep_as="servicenow/templates.json")
        by_id = {value(r, "sys_id"): {"sys_id": value(r, "sys_id"), "name": value(r, "name"),
                                      "active": str(value(r, "active")).lower() == "true"} for r in rows}
        return {value(v, "sys_id"): by_id.get(value(v, "std_change_producer")) for v in versions}

    def attachments(self, change_ids):
        if not change_ids:
            return []
        return self.query("sys_attachment", f"table_name=change_request^table_sys_idIN{','.join(change_ids)}",
                          "sys_id,table_sys_id,file_name,size_bytes,sys_created_on",
                          keep_as="servicenow/attachments.json")

    def download(self, sys_id):
        request = urllib.request.Request(f"{self.sn.url}/api/now/attachment/{sys_id}/file",
                                         headers={"Authorization": self.sn.auth, "Accept": "*/*"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    def download_all(self, rows, workers=8):
        """{attachment sys_id: bytes}, eight at a time."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            ids = [value(row, "sys_id") for row in rows]
            return dict(zip(ids, pool.map(self.download, ids)))


class AwsReader:
    """CloudTrail and Lambda, as the evidence reader role or the session given."""

    def __init__(self, session, function, sleep=time.sleep):
        self.cloudtrail = session.client("cloudtrail")
        self.lambda_ = session.client("lambda")
        self.function = function
        self.sleep = sleep
        self.read = {}

    def alias_moves(self, start, end):
        """Every UpdateAlias of the function's live alias between start and end, oldest first:
        [{"time", "version", "event_id", "who"}]. One paged lookup."""
        moves, raw, token = [], [], None
        while True:
            args = {"LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": UPDATE_ALIAS}],
                    "StartTime": start, "EndTime": end, "MaxResults": 50}
            if token:
                args["NextToken"] = token
            reply = self.cloudtrail.lookup_events(**args)
            for event in reply.get("Events", []):
                detail = json.loads(event["CloudTrailEvent"])
                request = detail.get("requestParameters") or {}
                if function_name(request.get("functionName")) != self.function or request.get("name") != "live" or detail.get("errorCode"):
                    continue
                raw.append(redacted(detail))
                moves.append({"time": event["EventTime"].astimezone(datetime.timezone.utc),
                              "version": str(request.get("functionVersion")), "event_id": event["EventId"],
                              "who": (detail.get("userIdentity") or {}).get("arn", "").rsplit("/", 2)[-2:]})
            token = reply.get("NextToken")
            if not token:
                break
            self.sleep(CLOUDTRAIL_PAUSE)
        self.read["cloudtrail/update-alias-events.json"] = raw
        return sorted(moves, key=lambda move: move["time"])

    def version(self, number):
        """A Lambda version's configuration, its environment's values removed as they are read."""
        config = self.lambda_.get_function_configuration(FunctionName=self.function, Qualifier=number)
        config.pop("ResponseMetadata", None)
        if config.get("Environment", {}).get("Variables"):
            config["Environment"]["Variables"] = {name: "(value removed)" for name in config["Environment"]["Variables"]}
        self.read[f"lambda/{self.function}-{number}.json"] = config
        return config


def function_name(text):
    """orders-service-production, from a name or an ARN as the caller passed it."""
    text = str(text or "")
    return text.split(":function:", 1)[1].split(":", 1)[0] if ":function:" in text else text


def redacted(detail):
    """A CloudTrail event as read, without the caller's address and access key id."""
    copy = json.loads(json.dumps(detail, default=str))
    copy.pop("sourceIPAddress", None)
    identity = copy.get("userIdentity") or {}
    if "accessKeyId" in identity:
        identity["accessKeyId"] = "(removed)"
    return copy


class JFrogReader:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token
        self.read = {}

    def stored(self, path):
        """JFrog's storage record of a file: its SHA-256 among the checksums. None when JFrog has no such file."""
        request = urllib.request.Request(f"{self.base}/artifactory/api/storage/{urllib.parse.quote(path)}",
                                         headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                record = json.loads(response.read())
        except urllib.error.HTTPError as error:
            record = {"status": error.code}
        self.read[f"jfrog/{path.replace('/', '_')}.json"] = record
        return record


def assume(session, account):
    import boto3

    creds = session.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/{EVIDENCE_ROLE}",
                                              RoleSessionName=f"evidence-sample-{int(time.time())}")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=session.region_name)
