"""What the rollout tells everyone: each server's result in words, the Slack
line, the change's work note and affected CIs, and one Splunk and Grafana
event per server step. When the rollout stops, the shared pipeline's own stop
closes the change and posts the final line.

Only the tools host sends these (the data center hands its results back).
The shared scripts are used as they are: notify, change and events.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import run_state  # noqa: E402

RESULT_WORDS = {"ok": "ok", "rolled-back": "rolled back", "failed": "failed", "rollback-failed": "rollback failed",
                "not-started": "not started"}


def rows(servers, results, version):
    """One row per server in rollout order, with not-started for the ones the play never reached."""
    found = {r["server"]: r for r in results}
    table = []
    for server in servers:
        result = found.get(server["name"])
        if result is None:
            table.append({"server": server["name"], "site": server["site"], "location": server["location"],
                          "result": "not-started", "target": version, "running": None, "previous": None,
                          "seconds": None, "detail": "", "steps": []})
        else:
            table.append({**result, "seconds": round(result["finished_at"] - result["started_at"], 1)})
    return table


def stopped_at(table):
    return next((row for row in table if row["result"] in ("failed", "rolled-back", "rollback-failed")), None)


def line(row):
    """One server's result in a sentence, for the change and the log."""
    name = f"{row['server']} ({row['location']})"
    seconds = f" in {row['seconds']:.0f}s" if row.get("seconds") is not None else ""
    if row["result"] == "ok":
        return f"{name}: on {row['running']}, check ok{seconds}. {why(row)}".strip()
    if row["result"] == "rolled-back":
        return (f"{name}: {row['target']} failed its check; rolled back to {row['running']} and checked{seconds}. "
                f"{why(row)}").strip()
    if row["result"] == "rollback-failed":
        return f"{name}: {row['target']} failed its check, and so did {row['previous']} after the rollback. {why(row)}"
    if row["result"] == "failed":
        return f"{name}: failed before the switch, still on {row['previous'] or 'nothing'}. {why(row)}"
    return f"{name}: not started, the rollout stopped before it."


def why(row):
    """The check's own words, without its ok or FAIL prefix."""
    return row.get("detail", "").removeprefix("FAIL ").removeprefix("ok   ").strip()


def slack_line(table):
    """The line the video quotes, or what happened instead."""
    row = stopped_at(table)
    if row is None:
        return None
    if row["result"] == "rolled-back":
        return f"{row['server']} failed its check. Rolled back to {row['running']}. Rollout stopped."
    if row["result"] == "rollback-failed":
        return f"{row['server']} failed its check, and {row['previous']} fails its check too. Rollout stopped. Look now."
    return f"{row['server']} failed before the switch. Still on {row['previous'] or 'nothing'}. Rollout stopped."


def finish_line(table):
    """The pipeline's last Slack line for a stopped rollout; {total} is filled in by notify.py."""
    row = stopped_at(table)
    untouched = [r["server"] for r in table if r["result"] == "not-started"]
    tail = f" {', '.join(untouched)} untouched." if untouched else ""
    return f"Rollout stopped at {row['server']} after {{total}}.{tail}"


def summary(table, version):
    head = f"Rollout of inventory {version}, one server at a time:"
    body = [f"{i}. {line(row)}" for i, row in enumerate(table, 1)]
    row = stopped_at(table)
    unreached = next((r for r in table if r["result"] == "not-started"), None)
    if row:
        tail = f"Rollout stopped at {row['server']}."
    elif unreached:
        # A site's run failed before its first server, such as the data-center job on dc-agent.
        tail = f"Rollout stopped before {unreached['server']}: its run failed before reaching it (see above)."
    else:
        tail = f"All {len(table)} servers run {version}."
    return "\n".join([head, *body, tail])


def events(state, table, environment="production"):
    """One event per server step to Splunk and Grafana, then one per server's result."""
    import events as shared_events
    from common import ssm_client

    ssm = ssm_client()

    class Args:
        stage = "Production"
        seconds = None
        deploy = False
        recovery_seconds = None

    Args.environment = environment
    sent = 0
    for row in table:
        base = {"server": row["server"], "site": row["site"], "location": row["location"]}
        for step in row.get("steps", []):
            Args.result = "failed" if step["what"] in ("failed", "check failed") else "ok"
            body = {**shared_events.event(Args, state), **base, "step": step["what"], "detail": step.get("detail", ""),
                    "at": run_state.iso(step["at"])}
            shared_events.to_splunk(ssm, body)
            shared_events.to_loki(ssm, body)
            sent += 1
        Args.result = row["result"]
        Args.seconds = row.get("seconds")
        body = {**shared_events.event(Args, state), **base, "step": "result", "running": row.get("running"),
                "previous": row.get("previous"), "detail": row.get("detail", "")}
        shared_events.to_splunk(ssm, body)
        shared_events.to_loki(ssm, body)
        sent += 1
    print(f"Sent {sent} server events to Splunk and Grafana")


def servicenow():
    import change

    return change.connect()[0]


def affected_cis(state, names):
    """The servers as affected CIs on the change, once each."""
    change_record = state.get("change")
    if not change_record:
        return
    sn = servicenow()
    for name in names:
        ci = sn.find("cmdb_ci_linux_server", f"name={name}", "sys_id")
        if ci is None:
            print(f"No Linux Server CI {name} in ServiceNow: run scripts/demos/linux/servicenow_setup.py")
            continue
        query = f"task={change_record['sys_id']}^ci_item={ci['sys_id']}"
        if not sn.find("task_ci", query, "sys_id"):
            sn.call("POST", "/api/now/table/task_ci", {"task": change_record["sys_id"], "ci_item": ci["sys_id"]})


def note(state, text):
    import change

    if state.get("change"):
        change.note(servicenow(), state, text)


def post_line(text):
    """The Slack line, through the shared notify.py, before the pipeline's own stop."""
    import subprocess

    subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "notify.py"), "line", text], check=False)
