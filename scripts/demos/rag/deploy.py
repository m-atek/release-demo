"""Stages 6 and 10 for the runbook assistant: point an environment at the exact stored package.

    python scripts/demos/rag/deploy.py runbook-assistant staging
    python scripts/demos/rag/deploy.py runbook-assistant production --back       back to this run's previous pointer
    python scripts/demos/rag/deploy.py runbook-assistant production --previous   back to the release before (/rollback)

Downloads the package from JFrog Container Registry and checks its SHA-256
against the one recorded at build. Makes sure its search index exists and is
online (index.py); with the endpoint down it fails at once, "The search
endpoint is down: scripts/demo.sh up rag." Then it writes the environment's
pointer, {version, sha256, package, index, build, at}, through the deploy
role, and the run state's deploys entry. A deploy is a pointer move: the
answer path reads the pointer on every question. --previous takes the last
other version from the pointer's SSM history, so /rollback takes seconds.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402
import run_state  # noqa: E402
from common import fail, get_parameter, ssm_client  # noqa: E402


def pointer_for(state, index, environment):
    package = state["package"]
    return {"version": state["version"], "sha256": package["sha256"], "package": package["path"], "index": index,
            "build": state["build"]["number"], "at": run_state.iso(time.time()), "environment": environment}


def deploy(environment, state):
    import index as search_index

    folder = rag.fetch(state["package"]["path"], state["package"]["sha256"], get_parameter(ssm_client(), "jcr/token"))
    print(f"SHA-256 verified: {state['package']['sha256']}")
    name = search_index.ensure(folder, sha256=state["package"]["sha256"])
    previous = rag.read_pointer(environment)
    pointer = pointer_for(state, name, environment)
    rag.write_pointer(environment, pointer)
    state.setdefault("deploys", {})[environment] = {
        "number": state["version"], "previous_number": (previous or {}).get("version"),
        "previous_version": (previous or {}).get("version"), "previous_pointer": previous, "index": name,
        "url": "", "deployed_at": run_state.now()}
    run_state.save(state)
    print(f"{environment}: runbook-assistant {state['version']} on {name}, was "
          f"{(previous or {}).get('version') or 'nothing'}")


def back(environment, state):
    entry = state.get("deploys", {}).get(environment) or {}
    previous = entry.get("previous_pointer")
    if not previous:
        fail(f"{environment} ran no version before this run: nothing to go back to.")
    rag.write_pointer(environment, previous)
    entry.update({"rolled_back_to": previous["version"], "rolled_back_number": previous["version"]})
    run_state.save(state)
    print(f"{environment}: back on runbook-assistant {previous['version']}")


def previous(environment, state):
    history = rag.pointer_history(environment)
    target = rag.previous_pointer(history)
    if not target:
        fail(f"{environment} has no earlier release to go back to.")
    current = history[-1]
    rag.write_pointer(environment, {**target, "at": run_state.iso(time.time())})
    state.setdefault("deploys", {})[environment] = {
        "previous_number": current["version"], "previous_version": current["version"],
        "rolled_back_to": target["version"], "rolled_back_number": target["version"], "url": "",
        "deployed_at": run_state.now()}
    run_state.save(state)
    print(f"{environment}: back on runbook-assistant {target['version']}, was {current['version']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment", choices=["staging", "production"])
    moves = parser.add_mutually_exclusive_group()
    moves.add_argument("--back", action="store_true", help="back to this run's previous pointer")
    moves.add_argument("--previous", action="store_true", help="back to the release before the live one")
    args = parser.parse_args()
    if args.service != rag.SERVICE:
        fail(f"This deploys {rag.SERVICE}, not {args.service}.")
    state = run_state.load()
    if args.back:
        back(args.environment, state)
    elif args.previous:
        previous(args.environment, state)
    else:
        deploy(args.environment, state)


if __name__ == "__main__":
    main()
