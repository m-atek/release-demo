"""/sbom in Slack (video 24): which live service holds a CVE, read from the SBOM stored with what is live.

    VULN=CVE-2026-111497 ASKED_BY=U... REQUESTED_AT=1793000000 SLACK_CHANNEL=C... SLACK_TS=1793.0001 \\
        python3 scripts/demos/patch_clock/search.py

Run by the Jenkins job sbom-search, which the Slack handler starts as release-bot. The id arrives
as an environment variable, is checked here again, and reaches Grype as one word of an argument
list. It reads and reports. It imports none of change, deploy, stage or publish's upload (a test
says so); the only thing it writes, with the switch on and a holder found, is the clock's file.

1. Facts from Grype's own database, never typed by hand: `grype db search --vuln` on the pinned
   image with the shared cache. That command looks by name only, and GitHub's record is named for
   its GHSA id, so for a CVE the GHSA id comes from the database's alias table. Severity, published
   date and known exploited come from GitHub's record, package, range and fixed version from its
   packages. An id the database doesn't know is said, and nothing is searched.
2. For each service with live: in services.yaml, through the role release-demo-sbom-reader: the
   live alias's version, its description (the app version) and its checksum, or digest for an image.
3. With jcr/evidence-reader-token: the release folder whose build-info.json names that package,
   and the sbom.cdx.json stored in it, which sbom-info.json must tie to that package.
4. `grype sbom:<file> --by-cve -o json`, the database's auto-update off, up to three at once. A match
   on the id means the service holds it. A row reads clear only after that real run on a matching SBOM.
5. It rewrites the message the Slack handler posted. Every row that can't be trusted says why.
6. With the switch on and at least one holder, it opens the clock.
"""

import concurrent.futures
import datetime
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import clock  # noqa: E402
import live as readers  # noqa: E402
import notify  # noqa: E402
import run_state  # noqa: E402
import tools  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402
from switch import SWITCH  # noqa: E402
from tools import normalized  # noqa: E402

CVE = re.compile(r"CVE-[0-9]{4}-[0-9]+")
GHSA = re.compile(r"ghsa-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}")
AT_ONCE = 3
HOLDS, CLEAR, WARNING = "holds", "clear", "warning"
ICON = {HOLDS: ":x:", CLEAR: ":white_check_mark:", WARNING: ":warning:"}


def vulnerability_id(text):
    """The id as Grype and GitHub write it: CVE-2026-111497, GHSA-9v8h-qqgm-4m35. None for anything else."""
    text = (text or "").strip()
    if CVE.fullmatch(text.upper()):
        return text.upper()
    if GHSA.fullmatch(text.lower()):
        return "GHSA" + text.lower()[4:]
    return None


# 1. The facts.

def ghsa_ids(cve, status):
    """The GHSA ids that name this CVE as an alias, from the database's alias table.
    `grype db search` takes no alias flag. Unreadable means none, and the search goes on."""
    try:
        connection = sqlite3.connect(f"file:{tools.db_file(status)}?mode=ro&immutable=1", uri=True)
        try:
            rows = connection.execute("SELECT name FROM vulnerability_aliases "
                                      "WHERE alias = ? COLLATE NOCASE AND name LIKE 'GHSA-%' ORDER BY name", (cve,)).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, KeyError, ValueError) as error:
        print(f"The database's alias table not read ({error.__class__.__name__}: {error}): no GHSA ids for {cve}")
        return []
    return [name for (name,) in rows]


def cpe_product(cpe):
    """The product of a CPE: brotli in cpe:2.3:a:google:brotli:*:... An NVD row names no package."""
    parts = (cpe or "").split(":")
    return parts[4] if len(parts) > 4 else None


def affected_packages(matches):
    """[{name, range, fixed, unfixed}] from the packages view, GitHub's when it has any, by name.
    A row that says nothing of a fix, as NVD's don't, leaves both empty: the reply then says nothing either."""
    github = [match for match in matches if match["vulnerability"].get("provider") == "github"]
    found = {}
    for match in github or matches:
        for affected in match.get("packages") or []:
            name = (affected.get("package") or {}).get("name") or cpe_product(affected.get("cpe"))
            if not name:
                continue
            entry = found.setdefault(normalized(name), {"name": name, "range": [], "fixed": [], "unfixed": False})
            for item in (affected.get("detail") or {}).get("ranges") or []:
                constraint = ((item.get("version") or {}).get("constraint") or "").replace(" ", "")
                fix = item.get("fix") or {}
                if constraint and constraint not in entry["range"]:
                    entry["range"].append(constraint)
                if fix.get("version") and fix["version"] not in entry["fixed"]:
                    entry["fixed"].append(fix["version"])
                entry["unfixed"] = entry["unfixed"] or fix.get("state") in ("not-fixed", "wont-fix")
    return [found[name] for name in sorted(found)]


def published_on(text):
    """22 Oct 2026, from Grype's RFC 3339 time."""
    try:
        day = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{day.day} {day:%b %Y}"


def facts_from(vulnerability, matches, records, also=()):
    """What the reply says of the vulnerability, from `db search --vuln` and `db search vuln`. None when unknown."""
    all_records = [match["vulnerability"] for match in matches] + list(records)
    if not all_records:
        return None
    github = [record for record in all_records if record.get("provider") == "github"]
    record = (github or all_records)[0]
    severity = (record.get("severity") or "").capitalize()
    # What a match may be named: the id, the GHSA ids that alias a CVE, or the CVEs a GHSA names.
    ids = {vulnerability, *also}
    if not vulnerability.startswith("CVE-"):
        for item in all_records:
            ids.update(item.get("aliases") or [])
    packages = affected_packages(matches)
    return {"severity": "" if severity == "Unknown" else severity,
            "known_exploited": any(item.get("known_exploited") for item in all_records),
            "published": published_on(record.get("published_date")),
            "packages": packages, "names": {normalized(p["name"]) for p in packages},
            "ids": {name.upper() for name in ids}}


def read_facts(vulnerability):
    status = tools.usable_database()
    also = ghsa_ids(vulnerability, status) if vulnerability.startswith("CVE-") else []
    matches = tools.db_search(vulnerability, *also)
    records = tools.db_records(vulnerability) if not matches else []
    return facts_from(vulnerability, matches, records, also)


# 2 to 4. One service.

@dataclass
class Row:
    service: str
    version: str
    kind: str
    says: str
    held: tuple = ()      # the (package, version) pairs it holds
    named: str = ""       # a clear row's package the advisory names


def warning(service_name, version, says):
    return Row(service_name, version, WARNING, says)


def error_name(error):
    """AWS's own name for what went wrong, JFrog's status, or the error's class: nothing a token could be in."""
    if isinstance(error, readers.JFrogAnswered):
        return str(error)
    return (getattr(error, "response", None) or {}).get("Error", {}).get("Code") or error.__class__.__name__


def verdict(service_name, version, report, sbom, facts):
    """A real Grype run on a matching SBOM: holds it, or clear."""
    held = {}
    for match in report["matches"]:
        found = {match["vulnerability"]["id"], *(item.get("id", "") for item in match.get("relatedVulnerabilities") or [])}
        if facts["ids"] & {name.upper() for name in found}:
            artifact = match["artifact"]
            held[(artifact["name"], artifact["version"])] = None
    if held:
        (name, held_version), *more = held
        also = f" and {len(more)} more" if more else ""
        return Row(service_name, version, HOLDS, f"{service_name} {version} holds {name} {held_version}{also}", tuple(held))
    components = sbom.get("components") or []
    named = [c for c in components if normalized(c.get("name")) in facts["names"]]
    if named:
        says = f"{service_name} {version}: clear, {named[0]['name']} {named[0].get('version', '')}".rstrip()
        return Row(service_name, version, CLEAR, says, named=named[0]["name"])
    count = len(components)
    return Row(service_name, version, CLEAR, f"{service_name} {version}: clear, {count} component{'' if count == 1 else 's'}")


def usable(reader):
    """A role or a token that couldn't be had is an error in place of its client: each row reports it."""
    if isinstance(reader, Exception):
        raise reader
    return reader


def searched(service_name, block, client, jfrog, facts):
    """One service's row. Only verdict() can say clear, and only after a real Grype run on its stored SBOM."""
    try:
        what = readers.read_live(usable(client), block)
    except Exception as error:
        return warning(service_name, "", f"{service_name}: couldn't read what's live ({error_name(error)})")
    try:
        sbom, why_not = readers.stored_sbom(usable(jfrog), service_name, what)
    except Exception as error:
        return warning(service_name, what.version, f"{service_name} {what.version}: couldn't read JFrog ({error_name(error)})")
    if why_not == readers.NONE_STORED:
        return warning(service_name, what.version, f"{service_name} {what.version}: no SBOM stored with this package")
    if why_not == readers.NOT_THIS_PACKAGE:
        return warning(service_name, what.version, f"{service_name} {what.version}: the stored SBOM doesn't match the live package")
    try:
        with tools.scratch() as folder:
            (folder / "sbom.cdx.json").write_bytes(sbom)
            report = tools.scan(folder / "sbom.cdx.json")
            return verdict(service_name, what.version, report, json.loads(sbom), facts)
    except Exception as error:
        return warning(service_name, what.version, f"{service_name} {what.version}: Grype couldn't read its SBOM ({error_name(error)})")


def rows_for(services, client, jfrog, facts):
    """In services.yaml's order, up to three at once."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        return list(pool.map(lambda item: searched(item[0], item[1], client, jfrog, facts), services.items()))


# 5. The reply.

def head(vulnerability, facts, asked_by):
    parts = [f"*{vulnerability}*", facts["severity"], "known exploited" if facts["known_exploited"] else "",
             f"asked by {notify.requester(asked_by)}"]
    return " · ".join(part for part in parts if part)


def advisory_line(facts, rows):
    """package range · fixed in x · published date. An advisory may name several packages: the
    one a row holds or names comes first, so the line is about what the rows say."""
    packages = facts["packages"]
    held = {normalized(name) for row in rows for name, _ in row.held}
    named = {normalized(row.named) for row in rows if row.named}
    shown = next((p for p in packages if normalized(p["name"]) in held), None) or \
        next((p for p in packages if normalized(p["name"]) in named), None) or (packages[0] if packages else None)
    parts = []
    if shown:
        parts.append(" ".join(filter(None, [shown["name"], " || ".join(shown["range"])])))
        if shown["fixed"]:
            parts.append(f"fixed in {' or '.join(shown['fixed'])}")
        elif shown["unfixed"]:
            parts.append("no fixed version yet")
    if facts["published"]:
        parts.append(f"published {facts['published']}")
    return " · ".join(parts)


def summary(rows):
    holders = sum(1 for row in rows if row.kind == HOLDS)
    unknown = sum(1 for row in rows if row.kind == WARNING)
    said = f"{holders} of {len(rows)} {'holds' if holders == 1 else 'hold'} it."
    return f"{said} {unknown} couldn't be searched." if unknown else said


def slack_escaped(text):
    """Slack reads &, < and > as its own marks: a range such as <1.2.1 || >=2.0 would turn into a link. It shows these as typed."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def reply(vulnerability, facts, asked_by, rows, seconds, others):
    count = len(rows)
    text = ["\n".join(line for line in (head(vulnerability, facts, asked_by), slack_escaped(advisory_line(facts, rows))) if line),
            "\n".join([f"Searched the SBOMs of {count} live service{'' if count == 1 else 's'} in {run_state.duration(seconds)}:",
                       *(f"{ICON[row.kind]}  {row.says}" for row in rows)]),
            summary(rows)]
    if others:
        text.append(f"Not searched: {', '.join(others)}.")
    return "\n\n".join(text)


def unknown_id(vulnerability, asked_by):
    return f"*{vulnerability}* · asked by {notify.requester(asked_by)}\n\nGrype's database doesn't know {vulnerability}. Nothing searched."


def failed(vulnerability, asked_by, error):
    return (f"*Search for {vulnerability}* · asked by {notify.requester(asked_by)}\n\n"
            f":x:  The search failed ({error_name(error)}). The job's log says why.")


def update(text):
    channel, ts = os.environ.get("SLACK_CHANNEL", ""), os.environ.get("SLACK_TS", "")
    print(text)
    if channel and ts:
        notify.slack("chat.update", get_parameter(ssm_client(), "slack/bot-token"), channel=channel, ts=ts, text=text)


# 6. The clock, and the run.

def holders_of(rows):
    return {row.service: {"package": row.held[0][0], "version": row.held[0][1]} for row in rows if row.kind == HOLDS}


class NoJFrogToken(Exception):
    """jcr/evidence-reader-token isn't in SSM yet."""


def lambda_or_error():
    """release-demo-sbom-reader's client, or the error every row then reports."""
    try:
        return readers.reader_client()
    except Exception as error:
        return error


def jfrog_or_error():
    try:
        return readers.evidence_reader()
    except SystemExit:
        return NoJFrogToken()
    except Exception as error:
        return error


def search(vulnerability, asked_by, requested_at):
    """The reply's text, once everything is read. Opens the clock when it should."""
    facts = read_facts(vulnerability)
    if facts is None:
        return unknown_id(vulnerability, asked_by)
    rows = rows_for(readers.searchable(), lambda_or_error(), jfrog_or_error(), facts)
    holders = holders_of(rows)
    if holders and SWITCH.is_on():
        try:
            opened = clock.start(vulnerability, requested_at, holders, facts["ids"])
            print(f"The clock for {vulnerability} runs from {run_state.iso(opened['requested_at'])}")
        except OSError as error:  # the answer still goes out; a rehearsal shows the missing line
            print(f"The clock didn't open ({error.__class__.__name__}: {error})")
    return reply(vulnerability, facts, asked_by, rows, run_state.now() - requested_at, readers.not_searched())


def main():
    asked_by = os.environ.get("ASKED_BY", "")
    vulnerability = vulnerability_id(os.environ.get("VULN"))
    if not vulnerability:
        update("Usage: /sbom <CVE or GHSA id>, for example /sbom CVE-2026-111497")
        return 1
    requested_at = run_state.requested_at(os.environ.get("REQUESTED_AT"))
    try:
        update(search(vulnerability, asked_by, requested_at))
    except Exception as error:  # the message says it failed; it never stays on its waiting line
        print(f"{error.__class__.__name__}: {error}", file=sys.stderr)
        update(failed(vulnerability, asked_by, error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
