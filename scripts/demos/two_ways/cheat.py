"""Video 28's two cheats, each done as someone with admin rights would, and the read that proves what they reached.

    python scripts/demos/two_ways/cheat.py approve CHG0030120   an approval in the requester's name, as ServiceNow's admin
    python scripts/demos/two_ways/cheat.py swap 123             the stored package of build 123 overwritten, as JFrog's admin
    python scripts/demos/two_ways/cheat.py production           what production runs: read only, for before and after

The pipeline itself does neither. Its refusals are the pipeline's own code: change.py's wait and deploy.py's checksum.

approve runs where you are signed in: the tools host may not read servicenow/admin-password. It adds one row to the
change's approvals, approved, with the person who asked as the approver, and leaves Alex's row alone. Sam never signs in
and clicks nothing: the record says he approved, and the row's creator says who wrote it.

swap needs JFrog on the tools host's localhost, so from the Mac it hands itself over, as reset.py does. It first tries the
pipeline's own token, which must be refused: that is why this takes an admin. Then it stores the same package with one
harmless file added, so the checksum changes and nothing in it could hurt if a deploy ever took it.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(1, str(SCRIPTS))

import host  # noqa: E402
import publish  # noqa: E402
from change import MACHINE_USERS  # noqa: E402
from common import aws_session, fail, get_parameter, service, ssm_client  # noqa: E402
from servicenow_setup import SETUP_USER, ServiceNow, raw  # noqa: E402

SERVICE, VERSION = "orders-service", "1.30.0"
SCRIPT = "demos/two_ways/cheat.py"
EXTRA_FILE = "added-after-publish.txt"
EXTRA_TEXT = "Added after this package was published.\n"
WAITING = "-4"  # Assess: a normal change asks its approvers here
OVERWRITTEN, REFUSED = (200, 201), (403, 409)


def admin_servicenow():
    ssm = ssm_client()
    return ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER, get_parameter(ssm, "servicenow/admin-password"))


def approve(sn, number):
    """One row in the requester's name. Safe to run twice: the row is found again, not added again."""
    change = sn.find("change_request", f"number={number}", "sys_id,state,approval,requested_by")
    if not change:
        fail(f"ServiceNow has no change {number}.")
    if change["state"] != WAITING:
        fail(f"{number} isn't waiting for approval (state {change['state']}, approval {change['approval']}).")
    requester = raw(change["requested_by"])
    person = sn.find("sys_user", f"sys_id={requester}", "name,user_name") if requester else None
    if not person:
        fail(f"{number} names nobody as Requested by.")
    if person["user_name"] in MACHINE_USERS:
        fail(f"{number} was asked by {person['user_name']}, not a person: this run didn't start from Sam's Slack.")
    query = f"sysapproval={change['sys_id']}^approver={requester}"
    row = sn.find("sysapproval_approver", query, "sys_id,state")
    if not row:
        row = sn.call("POST", "/api/now/table/sysapproval_approver", {
            "sysapproval": change["sys_id"], "source_table": "change_request", "document_id": change["sys_id"],
            "approver": requester, "state": "approved"})
    if row["state"] != "approved":
        sn.call("PATCH", f"/api/now/table/sysapproval_approver/{row['sys_id']}", {"state": "approved"})
    stored = sn.find("sysapproval_approver", query, "sys_id,state,sys_created_by")
    if not stored or stored["state"] != "approved":
        fail(f"{number}: ServiceNow doesn't show the approval in {person['name']}'s name: {stored}.")
    print(f"{number}: asked by {person['name']} ({person['user_name']}).")
    print(f"Approval in {person['name']}'s name: {stored['state']}, row written by {stored['sys_created_by']}.")
    return stored


def package_path(build, version=VERSION):
    return f"{publish.candidates_root(SERVICE)}/{version}/{build}/{SERVICE}-{version}.zip"


def with_one_more_file(package):
    """The same zip, every file as it was, plus one harmless text file."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as old, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as new:
        for item in old.infolist():
            new.writestr(item, old.read(item.filename))
        new.writestr(EXTRA_FILE, EXTRA_TEXT)
    return out.getvalue()


def checksums(content):
    return {"X-Checksum-Sha256": hashlib.sha256(content).hexdigest(), "X-Checksum-Sha1": hashlib.sha1(content).hexdigest(),
            "Content-Type": "application/octet-stream"}


def jcr_as_admin():
    """scripts/reset.py's admin client, by its path: this folder has a reset.py of its own."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    return shared.jcr_request


def stored(request, path, password):
    """(SHA-256, who deployed it) from JFrog's own record, as the General tab shows them."""
    status, body = request("GET", f"api/storage/{path}", password)
    if status != 200:
        fail(f"JCR has no {path} ({status}).")
    info = json.loads(body)
    return info.get("checksums", {}).get("sha256"), info.get("modifiedBy") or info.get("createdBy") or "?"


def swap(build, version, ssm=None, request=None):
    """Exit 0 when the package is overwritten and the pipeline's token was refused; 3 if that token could do it."""
    ssm = ssm or ssm_client()
    request = request or jcr_as_admin()
    password = get_parameter(ssm, "jcr/admin-password")
    path = package_path(build, version)
    before, deployer = stored(request, path, password)
    print(f"{path}")
    print(f"Stored now: SHA-256 {before}, deployed by {deployer}.")
    status, package = request("GET", path, password)
    if status != 200:
        fail(f"JCR didn't give {path} ({status}).")
    if EXTRA_FILE in zipfile.ZipFile(io.BytesIO(package)).namelist():
        print(f"Already swapped: it holds {EXTRA_FILE}. Nothing written.")
        return 0
    changed = with_one_more_file(package)
    # What the story rests on: the pipeline may deploy new files, and may not overwrite one.
    status, body = publish.request("PUT", path, get_parameter(ssm, "jcr/token"), changed, checksums(changed))
    if status in OVERWRITTEN:
        print(f"The pipeline's own token overwrote it ({status}). The script's line about its token is wrong: stop and look.")
        return 3
    print(f"The pipeline's own token: refused, {status}." if status in REFUSED else
          f"The pipeline's own token: answered {status}, not 403. Read it before the script says \"refused\".")
    status, body = request("PUT", path, password, changed, checksums(changed))
    if status not in OVERWRITTEN:
        fail(f"JCR refused the admin's overwrite ({status}): {body[:200].decode(errors='replace')}")
    after, deployer = stored(request, path, password)
    if after == before or after != hashlib.sha256(changed).hexdigest():
        fail(f"JFrog's checksum didn't follow the overwrite: {after}.")
    print(f"Stored now: SHA-256 {after}, deployed by {deployer}.")
    return 0


def production(client=None):
    """What production runs and what it has published. Two reads that should match before and after a refusal."""
    client = client or aws_session().client("lambda")
    function = service(SERVICE)["environments"]["production"]["function"]
    live = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
    versions = []
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        versions += [v for v in page["Versions"] if v["Version"] != "$LATEST"]
    running = next((v.get("Description", "?") for v in versions if v["Version"] == live), "?")
    newest = max((int(v["Version"]) for v in versions), default=0)
    took = [v["Version"] for v in versions if v.get("Description") == VERSION]
    print(f"{function}: live is version {live} ({running}), newest published {newest}, "
          f"{len(took)} published as {VERSION}.")
    return {"live": live, "running": running, "newest": newest, "as_take_version": len(took)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    sub = parser.add_subparsers(dest="command", required=True)
    approver = sub.add_parser("approve", help="an approval in the requester's name")
    approver.add_argument("number", help="the change, CHG0030120")
    swapper = sub.add_parser("swap", help="overwrite the stored package of one build")
    swapper.add_argument("build")
    swapper.add_argument("--version", default=VERSION)
    sub.add_parser("production", help="what production runs, read only")
    args = parser.parse_args()

    if args.command == "approve":
        approve(admin_servicenow(), args.number)
        return 0
    if args.command == "production":
        production()
        return 0
    if not args.here and not host.on_host():
        return host.hand_over("two-ways-swap", SCRIPT, ["swap", args.build, "--version", args.version],
                              title=f"cheat.py swap {args.build}")
    sys.stdout.reconfigure(line_buffering=True)
    return swap(args.build, args.version)


if __name__ == "__main__":
    sys.exit(main())
