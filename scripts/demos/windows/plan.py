"""The change side of the wave plan: what Alex approves, once, before any server is touched.

    python scripts/demos/windows/plan.py store-sync      print the plan text of this run's plan

deploy.py staging makes the plan and, with this module, puts in the run state
what scripts/change.py open adds to the change while it is still New (in
Assess the change model makes affected items read-only):

    change_extras:
      fields:        implementation_plan (the waves), backout_plan, and for a
                     resume a short_description naming the servers and parent,
                     the last change for this release
      affected_cis:  the planned servers' CMDB sys_ids
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parents[1]) not in sys.path:
    sys.path.append(str(HERE.parents[1]))
import run_state  # noqa: E402
from common import fail, service  # noqa: E402

BACKOUT = ("Per server, automatic: when /health doesn't report the new version within 60 seconds, the pipeline "
           "switches the server back to the version it ran before, starts it and checks it. Both versions stay "
           "installed side by side, so going back needs no download.")


def plan_text(plan, env, target):
    lines = [f"store-sync {target} on the store fleet, in waves, through AWS Systems Manager Run Command. "
             "Nobody signs in to a server and no server accepts inbound connections."]
    for number, wave in zip(plan["numbers"], plan["waves"]):
        cells = [f"{name} ({plan['servers'][name]['store']}, on {plan['servers'][name]['before']})" for name in wave]
        lines.append(f"Wave {number}: {', '.join(cells)}")
    lines += [f"Never more than {plan['parallel']} servers at once.",
              "Per server: pre-checks (free disk, service state, pending reboot, the store's maintenance window); "
              f"download {target} from JFrog Container Registry and verify its SHA-256; stop the service; switch "
              f"to {target}; start; /health must report {target} within {env['check']['timeout_seconds']} seconds.",
              "Stop rule: when a server fails, the servers already running finish and no new server starts."]
    return "\n".join(lines)


def previous_change(sn, state):
    """The last change for this release, for a resume."""
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_limit=1, sysparm_fields="sys_id,number,close_code",
                   sysparm_query=f"short_descriptionSTARTSWITHRelease {state['service']} {state['version']} "
                                 f"^ORDERBYDESCsys_created_on")
    return rows[0] if rows else None


def change_extras(sn, config, state, plan):
    """What change.py open adds to the change: the plan's fields and its servers as affected items."""
    env = config["environments"]["production"]
    names = [n for wave in plan["waves"] for n in wave]
    table = config["servicenow"]["server_class"]
    cis = []
    for name in names:
        ci = sn.find(table, f"name={name}", "sys_id")
        if not ci:
            fail(f"ServiceNow has no {table} {name}. Run: python scripts/demos/windows/servicenow_fleet.py")
        cis.append(ci["sys_id"])
    fields = {"implementation_plan": plan_text(plan, env, state["version"]) + f"\nBuild: {state['build'].get('url', '')}",
              "backout_plan": BACKOUT}
    extras = {"fields": fields, "affected_cis": cis}
    if plan.get("resume"):
        fields["short_description"] = (f"Release {state['service']} {state['version']} ({state['jira_key']}), "
                                       f"resume: {', '.join(names)}")
        earlier = previous_change(sn, state)
        if earlier:
            fields["parent"] = earlier["sys_id"]
            extras["resumes"] = {"sys_id": earlier["sys_id"], "number": earlier["number"]}
    return extras


def main():
    config = service(sys.argv[1] if len(sys.argv) > 1 else "store-sync")
    state = run_state.load()
    if not state.get("plan"):
        fail("No plan in the run state: run deploy.py store-sync staging first.")
    print(plan_text(state["plan"], config["environments"]["production"], state["version"]))


if __name__ == "__main__":
    main()
