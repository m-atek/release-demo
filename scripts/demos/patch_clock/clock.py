"""The patch clock (video 24): from the first search that finds a holder to the closed change that removes the last one.

    python3 scripts/demos/patch_clock/clock.py stop     in Record, after change.py close successful
    python3 scripts/demos/patch_clock/clock.py list     what is open

Open: the first search that finds a holder writes /data/jenkins-agent/patch-clock/<id>.json
with the id, REQUESTED_AT and the holders. A later search keeps the first time and adds any new
holder; only a clean release takes one off. Stop: if this run's service is a holder and this
run's grype.json has no match on the id, it comes off. When the last one goes, Slack's status
message gets "[CHG number] closed. Search to closed change: [8m 40s]." and the change a work
note with the same time. With the switch off nothing opens or stops. It only ever prints what
goes wrong: the pipeline runs it with returnStatus, and it can't fail a release.
"""

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import run_state  # noqa: E402
from switch import SWITCH  # noqa: E402
from tools import normalized  # noqa: E402

FOLDER = Path(os.environ.get("PATCH_CLOCK_DIR", "/data/jenkins-agent/patch-clock"))


@contextmanager
def locked():
    """Several searches may run at once, and a release may stop the clock while one does."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    with open(FOLDER / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def path_of(vulnerability):
    return FOLDER / f"{vulnerability}.json"


def write(clock):
    temporary = path_of(clock["id"]).with_suffix(".tmp")
    temporary.write_text(json.dumps(clock, indent=2) + "\n")
    temporary.replace(path_of(clock["id"]))


def open_clocks():
    """Every open clock, oldest id first."""
    if not FOLDER.is_dir():
        return []
    return [json.loads(path.read_text()) for path in sorted(FOLDER.glob("*.json"))]


def start(vulnerability, requested_at, holders, ids=()):
    """Opens the clock, or keeps its first time and adds new holders. holders: {service: {"package", "version"}}."""
    with locked():
        path = path_of(vulnerability)
        clock = json.loads(path.read_text()) if path.exists() else {
            "id": vulnerability, "requested_at": requested_at, "ids": sorted(set(ids) | {vulnerability}), "holders": {}}
        clock["holders"] = {**holders, **clock["holders"]}
        write(clock)
    return clock


def clean(report_file, ids):
    """Has this run's scan no match on any of these ids? No report, or one that can't be read, is not clean."""
    try:
        matches = json.loads(Path(report_file).read_text())["matches"]
    except (OSError, ValueError, KeyError):
        return False
    wanted = {name.upper() for name in ids}
    for match in matches or []:
        found = {match["vulnerability"]["id"], *(related.get("id", "") for related in match.get("relatedVulnerabilities") or [])}
        if wanted & {name.upper() for name in found}:
            return False
    return True


def holds_now(sbom_file, package):
    """What this run's package holds of a package: "brotli 1.2.1", from its SBOM, or None."""
    try:
        components = json.loads(Path(sbom_file).read_text()).get("components") or []
    except (OSError, ValueError):
        return None
    return next((f"{c['name']} {c.get('version', '')}".strip() for c in components
                 if normalized(c.get("name")) == normalized(package)), None)


def work_note(state, text):
    import change

    sn, _ = change.connect()
    change.note(sn, state, text)


def slack_line(state, text):
    import notify

    state.setdefault("lines", []).append(text)
    run_state.save(state)
    notify.post(state)


def stop(state, reports=None, note=work_note, line=slack_line):
    """This run's release has closed its change: does it take a holder off, and was that the last?"""
    reports = reports or run_state.REPORTS
    service_name = state["service"]
    with locked():
        ended = []
        for clock in open_clocks():
            if service_name not in clock["holders"]:
                continue
            if not clean(reports / "grype.json", clock.get("ids") or [clock["id"]]):
                print(f"{clock['id']}: this scan isn't clean of it, so {service_name} stays a holder")
                continue
            held = clock["holders"].pop(service_name)
            if clock["holders"]:
                write(clock)
                print(f"{clock['id']}: {service_name} is off; {', '.join(sorted(clock['holders']))} still hold it")
                continue
            path_of(clock["id"]).unlink()
            ended.append((clock, held))
    for clock, held in ended:
        took = run_state.duration(run_state.now() - clock["requested_at"])
        now = holds_now(reports / "sbom.cdx.json", held["package"])
        holding = f"holds {now}" if now else f"no longer holds {held['package']}"
        number = state["change"]["number"]
        for send, text in ((line, f"{number} closed. Search to closed change: {took}."),
                           (note, f"{clock['id']}: {service_name} {state['version']} {holding}. Search to closed change: {took}.")):
            try:
                send(state, text)
                print(text)
            except (Exception, SystemExit) as error:  # the clock has stopped; a message that fails only says so here
                print(f"Patch clock: \"{text}\" not sent ({error.__class__.__name__})")
    return [clock["id"] for clock, _ in ended]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["stop", "list"])
    args = parser.parse_args()
    if args.action == "list":
        print(json.dumps(open_clocks(), indent=2))
        return 0
    if not SWITCH.is_on():
        return 0
    state = run_state.load()
    if state.get("service") and state.get("change"):
        stop(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
