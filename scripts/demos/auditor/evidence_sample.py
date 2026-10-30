"""The auditor picks ten changes: an evidence sample of orders-service's production changes.

    python scripts/demos/auditor/evidence_sample.py --from 2026-10-27 --to 2026-10-29 --size 10 --seed 4127
    python scripts/demos/auditor/evidence_sample.py --from 2026-10-27 --to 2026-10-29 --dry-run

Draws a sample of orders-service's changes that reached production in the
period and answers five questions for each from the original records:
ServiceNow's changes, approvals and attachments, JFrog's stored checksums,
and AWS's own record of the deploy (CloudTrail's UpdateAlias, Lambda's
CodeSha256). No AI anywhere in this path, and nothing is written anywhere
but the output folder: the pages, findings.json and its SHA-256 (the
findings hash, the same for the same seed and period), and a ZIP of every
record as read. The Jenkins job evidence-sample runs it on the tools host,
where JFrog answers and the role release-demo-evidence-reader may be
assumed; --dry-run also runs on the Mac and lists the population only.

The period is calendar days in Europe/Amsterdam, both included.
"""

import argparse
import datetime
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(0, str(HERE))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

import records  # noqa: E402
import report  # noqa: E402
import sample  # noqa: E402
from common import ROOT, aws_session, get_parameter, service, ssm_client  # noqa: E402
from records import display, utc, value  # noqa: E402

SERVICE = "orders-service"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def period(start_date, end_date):
    """UTC bounds of Amsterdam calendar days, both included."""
    start = datetime.datetime.combine(start_date, datetime.time(), AMSTERDAM)
    end = datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time(), AMSTERDAM)
    return start.astimezone(datetime.timezone.utc), end.astimezone(datetime.timezone.utc)


def default_dates(today=None):
    today = today or datetime.datetime.now(AMSTERDAM).date()
    return today - datetime.timedelta(days=7), today


def servicenow():
    from servicenow_setup import ServiceNow

    ssm = ssm_client()
    return ServiceNow(get_parameter(ssm, "servicenow/instance-url"), "evidence.reader",
                      get_parameter(ssm, "servicenow/evidence-reader-password"))


def aws_reader(function):
    """The evidence reader role on the tools host; on the Mac, your own session reads the same records."""
    import host

    session = aws_session()
    if host.on_host():
        session = records.assume(session, session.client("sts").get_caller_identity()["Account"])
    return records.AwsReader(session, function)


def latest_files(rows, data):
    """{change sys_id: {file name: bytes}}, the newest attachment of each name."""
    files = {}
    for row in sorted(rows, key=lambda r: value(r, "sys_created_on")):
        files.setdefault(value(row, "table_sys_id"), {})[value(row, "file_name")] = data[value(row, "sys_id")]
    return files


def release_it_went_back_to(change, changes):
    """The closed release change of the version a /rollback went back to, the latest before it."""
    back = sample.ROLLBACK.match(value(change, "short_description") or "")
    if not back:
        return None
    created = value(change, "sys_created_on")
    candidates = [c for c in changes if value(c, "sys_created_on") < created and str(value(c, "state")) == sample.CLOSED
                  and (m := sample.RELEASE.match(value(c, "short_description") or "")) and m.group("version") == back.group("back")]
    return max(candidates, key=lambda c: value(c, "sys_created_on")) if candidates else None


def build_row(change, moves, context):
    sn_url, config = context["sn_url"], context["config"]
    number, sys_id = value(change, "number"), value(change, "sys_id")
    template = context["templates"].get(value(change, "std_change_producer_version"))
    kind_ = sample.kind(change, template, config["servicenow"]["templates"])
    approvals = [a for a in context["approvals"] if value(a, "sysapproval") == sys_id]
    files = context["files"].get(sys_id, {})
    info = json.loads(files["build-info.json"]) if "build-info.json" in files else None
    release = sample.RELEASE.match(value(change, "short_description") or "")
    version = (info or {}).get("version") or (release.group("version") if release else "")
    deploy = moves[0]
    aws = context["aws"]
    versions = {m["version"]: aws.version(m["version"]).get("Description") for m in moves}

    change_link = (f"{sn_url}/change_request.do?sys_id={sys_id}", f"{number} in ServiceNow")
    first = sample.answer_approver(change, kind_, template, approvals)
    second = sample.answer_before(change, kind_, template, approvals, deploy)
    approved = [a for a in approvals if value(a, "state") == "approved"]
    for answer in (first, second):
        if kind_ == "normal":
            answer.links = [(f"{sn_url}/sysapproval_approver.do?sys_id={value(a, 'sys_id')}", "Approval record")
                            for a in approved[-1:]]
        elif template:
            answer.links = [(f"{sn_url}/std_change_record_producer.do?sys_id={template['sys_id']}",
                             f"Standard change template: {template['name']}")]

    links = [change_link, (report.splunk_link(number), f"Splunk: the events of {number}")]
    if kind_ == "rollback":
        went_back = release_it_went_back_to(change, context["all_changes"])
        target = ([(f"{sn_url}/change_request.do?sys_id={value(went_back, 'sys_id')}",
                    f"{value(went_back, 'number')}, the release it went back to")] if went_back else [])
        why = "A /rollback ships no package of its own: it moves production back to a version already released."
        third = sample.Answer(sample.BY_DESIGN, [why], target)
        fourth = sample.Answer(sample.BY_DESIGN, ["Its tests ran in the release it went back to."], target)
        fifth = sample.Answer(sample.BY_DESIGN, ["Its SBOM and scan are the release's it went back to."], target)
    else:
        from publish import candidates_root

        path = (f"{candidates_root(SERVICE)}/{info['version']}/{info['build']}/{info['package']}"
                if info and info.get("build") and info.get("package") else None)
        stored = context["jfrog"].stored(path) if path and context["jfrog"] else {}
        jfrog_sha = (stored.get("checksums") or {}).get("sha256")
        code = aws.version(deploy["version"]).get("CodeSha256")
        third = sample.answer_package(info, jfrog_sha, records_hex(code), path, deploy, aws.function)
        third.links = [link for link in [(report.jfrog_link(path), f"JFrog: {path}") if path else None,
                                         ((info or {}).get("build_url"), f"Jenkins build #{(info or {}).get('build')}")
                                         if (info or {}).get("build_url") else None] if link]
        fourth, fifth = sample.answer_tests(files), sample.answer_scan(files)
    if (info or {}).get("build_url"):
        links.append((info["build_url"], f"Jenkins build #{info.get('build')}"))
    return {"number": number, "version": version, "jira_key": release.group("key") if release else "",
            "kind": kind_, "kind_label": {"normal": "normal change", "standard": "standard change",
                                          "rollback": "rollback change"}[kind_],
            "template": template["name"] if template else None,
            "requested_by": display(change, "requested_by") or "",
            "approved_by": ", ".join(display(a, "approver") for a in approved) or "nobody",
            "close_code": display(change, "close_code") or "", "result": sample.result(change, kind_, moves, versions),
            "answers": [first, second, third, fourth, fifth], "links": links}


def records_hex(code):
    import evidence

    return evidence.base64_to_hex(code) if code else None


def write_zip(path, entries):
    """Every record as read, with fixed times: the same records give the same ZIP."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            data = entries[name]
            archive.writestr(info, data if isinstance(data, bytes) else json.dumps(data, indent=2, default=str))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_outputs(out, rows, header, rule, left_out, context, size, total, seed):
    (out / "evidence").mkdir(parents=True, exist_ok=True)
    (out / "evidence" / "style.css").write_text(report.STYLE)
    (out / "evidence-sample.html").write_text(report.index(rows, header, rule, left_out))
    for row in rows:
        (out / "evidence" / f"{row['number']}.html").write_text(report.change_page(row))
    found = json.dumps(sample.findings(rows), indent=2, sort_keys=True) + "\n"
    (out / "findings.json").write_text(found)
    findings_hash = sha256(found.encode())
    (out / "findings.sha256").write_text(f"{findings_hash}  findings.json\n")
    entries = {**context["reader"].read, **context["aws"].read, **(context["jfrog"].read if context["jfrog"] else {})}
    names = {value(r, "sys_id"): value(r, "number") for r in context["all_changes"]}
    for change_id, files in context["files"].items():
        for name, data in files.items():
            entries[f"attachments/{names.get(change_id, change_id)}/{name}"] = data
    archive = out / f"evidence-{seed}.zip"
    write_zip(archive, entries)
    description = f"Sample {size} of {total}, seed {seed}, findings {findings_hash[:8]}"
    (out / "description.txt").write_text(description + "\n")
    return findings_hash, sha256(archive.read_bytes()), description


def run(args, sn=None, aws=None, jfrog=None, out=None):
    started = time.monotonic()
    config = service(SERVICE)
    function = config["environments"]["production"]["function"]
    start, end = period(args.start, args.end)
    reader = records.ServiceNowReader(sn or servicenow())
    item = reader.item(config["servicenow"]["cmdb_item"])
    changes = reader.changes(item, start, end)
    aws = aws or aws_reader(function)
    now = datetime.datetime.now(datetime.timezone.utc)
    moves = aws.alias_moves(start - datetime.timedelta(hours=1), min(now, end + datetime.timedelta(days=1)))
    included, left_out = sample.population(changes, moves)
    if args.dry_run:
        print(f"{SERVICE}: {len(included)} changes reached production, {args.start} to {args.end}.")
        for change, change_moves in included:
            print(f"  {value(change, 'number')}  {value(change, 'short_description')}  "
                  f"({len(change_moves)} production move{'s' if len(change_moves) > 1 else ''})")
        print("Left out: " + ", ".join(f"{count} {why}" for why, count in left_out.items()) + ".")
        if args.seed is not None:
            chosen = [value(c, "number") for c, _ in sample.draw(included, args.size, args.seed)]
            print(f"Seed {args.seed} draws: {', '.join(chosen)}")
        return 0
    drawn = sample.draw(included, args.size, args.seed)
    header = (f"{SERVICE}: {len(included)} changes reached production, {args.start} to {args.end}. "
              f"Sample: {len(drawn)}, seed {args.seed}.")
    ids = [value(change, "sys_id") for change, _ in drawn]
    attachment_rows = reader.attachments(ids)
    context = {"sn_url": reader.sn.url, "config": config, "aws": aws, "reader": reader, "all_changes": changes,
               "approvals": reader.approvals(ids),
               "templates": reader.templates(sorted({value(c, "std_change_producer_version") for c, _ in drawn} - {None, ""})),
               "files": latest_files(attachment_rows, reader.download_all(attachment_rows)),
               "jfrog": jfrog if jfrog is not None else jfrog_reader()}
    rows = [build_row(change, change_moves, context) for change, change_moves in drawn]
    out = out or Path(args.out)
    findings_hash, zip_hash, description = write_outputs(out, rows, header, report.rule_text(args.seed, len(drawn)),
                                                         left_out, context, len(drawn), len(included), args.seed)
    print(header)
    print(report.summary_line(rows))
    print(f"Findings hash (SHA-256 of findings.json): {findings_hash}")
    print(f"evidence-{args.seed}.zip SHA-256: {zip_hash}")
    print(f"{description}. Done in {time.monotonic() - started:.0f}s.")
    return 0


def jfrog_reader():
    from publish import JCR

    return records.JFrogReader(JCR, get_parameter(ssm_client(), "jcr/evidence-reader-token"))


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    first, last = default_dates()
    day = datetime.date.fromisoformat
    parser.add_argument("--from", dest="start", type=day, default=first, help="first day, yyyy-mm-dd (default: 7 days ago)")
    parser.add_argument("--to", dest="end", type=day, default=last, help="last day, yyyy-mm-dd (default: today)")
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--seed", type=int, help="any whole number: the same seed and period draw the same changes")
    parser.add_argument("--dry-run", action="store_true", help="the population and what is left out; reads no attachment")
    parser.add_argument("--out", default=str(ROOT / "dist" / "evidence-sample"))
    args = parser.parse_args(argv)
    if args.seed is None and not args.dry_run:
        parser.error("--seed is required: any whole number, which anyone can use to draw the same sample again")
    if args.end < args.start:
        parser.error("--to is before --from")
    return args


def main(argv=None):
    return run(parse(argv))


if __name__ == "__main__":
    sys.exit(main())
