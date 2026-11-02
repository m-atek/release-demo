"""scripts/demo.sh up, down and status for video 24, the patch clock.

    python scripts/demos/patch_clock/demo.py up       the tools host answers, the three functions exist, Grype's database is warm
    python scripts/demos/patch_clock/demo.py down     the switch off; there is nothing to stop
    python scripts/demos/patch_clock/demo.py status   the switch, open clocks, what is live, the database's date, and a warning
                                                      if a version below the fix is live

Grype and the clock's files are on the tools host: from the Mac, those parts go through SSM Run
Command, as the other demos' checks do. Starting the tools host stays scripts/tools.sh's: status
only says it is stopped.
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import candidates  # noqa: E402
import clock  # noqa: E402
import host  # noqa: E402
import live as readers  # noqa: E402
import tools  # noqa: E402
from common import ROOT, aws_session, fail  # noqa: E402
from switch import SWITCH  # noqa: E402


def facts_here(warm=False):
    """What only the tools host knows, as a dict."""
    if warm:
        print(tools.db_update() or "updated", file=sys.stderr)
    status = tools.db_status()
    return {"built": status.get("built"), "valid": bool(status.get("valid")), "error": status.get("error"),
            "clocks": clock.open_clocks()}


def host_facts(warm=False):
    if host.on_host():
        return facts_here(warm)
    command = (f"cd {host.REPO_DIR} && (git pull --ff-only -q || true) && "
               f"{host.PYTHON} scripts/demos/patch_clock/demo.py on-host{' --warm' if warm else ''}")
    code, out, err = host.Host().shell(command, timeout=300)
    if code != 0:
        fail(f"The tools host didn't answer: {(err or out).strip()[-300:]}")
    return json.loads(out.strip().splitlines()[-1])


def lambda_reader():
    """On the tools host the search's own role; on the Mac your session, which that role doesn't trust."""
    return readers.reader_client() if host.on_host() else aws_session().client("lambda")


def live_now():
    """{service: Live, or the reason it can't be read}."""
    client = lambda_reader()
    found = {}
    for name, block in readers.searchable().items():
        try:
            found[name] = readers.read_live(client, block)
        except Exception as error:
            found[name] = f"can't read it ({error.__class__.__name__})"
    return found


def pin_below_the_fix(service_name, version):
    """The chosen package's pin in that version's demo state, when it is below the fix. None otherwise."""
    chosen = candidates.chosen()
    requirements = ROOT / "demo-states" / service_name / version / "requirements.txt"
    if not requirements.exists():
        return None
    for line in requirements.read_text().splitlines():
        pinned = re.fullmatch(rf"{re.escape(chosen.package)}==(\S+)", line.strip())
        if pinned and candidates.version_key(pinned.group(1)) < candidates.version_key(chosen.good):
            return pinned.group(1)
    return None


def warnings(found):
    chosen = candidates.chosen()
    lines = []
    for name, what in found.items():
        pin = None if isinstance(what, str) else pin_below_the_fix(name, what.version)
        if pin:
            lines.append(f"WARNING: {name} {what.version} is live and holds {chosen.package} {pin}, below {chosen.good}. "
                         "After the session: .venv/bin/python scripts/reset.py")
    return lines


def up():
    host_answers = host_facts(warm=True)
    print(f"Tools host: answers. Grype's database: built {host_answers['built']}, {'valid' if host_answers['valid'] else 'NOT valid: ' + str(host_answers['error'])}")
    found = live_now()
    unreadable = {name: what for name, what in found.items() if isinstance(what, str)}
    for name, what in found.items():
        print(f"{name}: {what if isinstance(what, str) else what.version}")
    if unreadable or not host_answers["valid"]:
        fail("Not ready: see the lines above.")
    print("Ready")
    return 0


def down():
    SWITCH.set("off")
    print("Nothing to stop: the search and the clock run only inside Jenkins jobs.")
    return 0


def status():
    print(f"Switch {SWITCH.name}: {SWITCH.value() or 'not readable'}")
    try:
        facts = host_facts()
        print(f"Grype's database: built {facts['built']}, {'valid' if facts['valid'] else 'NOT valid: ' + str(facts['error'])}")
        for opened in facts["clocks"]:
            print(f"Open clock {opened['id']}: from {opened['requested_at']}, held by {', '.join(sorted(opened['holders']))}")
        if not facts["clocks"]:
            print("Open clocks: none")
    except SystemExit:
        print("Tools host: not answering, so no clocks or database to read (scripts/tools.sh status).")
    found = live_now()
    for name, what in found.items():
        print(f"{name}: {what if isinstance(what, str) else f'{what.version} (Lambda version {what.lambda_version})'}")
    for line in warnings(found):
        print(line)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status", "on-host"])
    parser.add_argument("--warm", action="store_true", help="on-host: update Grype's database first")
    args = parser.parse_args()
    if args.action == "on-host":
        print(json.dumps(facts_here(args.warm)))
        return 0
    return {"up": up, "down": down, "status": status}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
