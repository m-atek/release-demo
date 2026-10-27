"""The wave logic of a fleet upgrade, without AWS: the plan, the pre-check
rules, the parallel limit, the stop rule and the texts. deploy.py and plan.py
use it; tests/ test it.

The stop rule: when a server fails, the servers already running finish, no
new server starts, and no later wave starts.
"""

import datetime
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from zoneinfo import ZoneInfo

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# What a Grafana tile shows for a server.
WAITING, UPGRADING, HEALTHY, FAILED, ROLLED_BACK = "waiting", "upgrading", "healthy", "failed", "rolled back"


def plan(waves, versions, target, resume=False):
    """The waves to run, as lists of server names.

    versions: {name: the version it runs now}. A normal release plans every
    server; one already on the target is skipped when its turn comes. Resume
    plans only the servers not on the target, in their waves, and drops waves
    left empty.
    """
    planned = []
    for wave in waves:
        servers = [s for s in wave if not (resume and versions.get(s) == target)]
        if servers:
            planned.append(servers)
    return planned


def in_window(window, timezone, now=None):
    """Is the store's maintenance window open? window: "Mon-Fri 22:00-06:00" or
    "Mon-Sun 00:00-24:00", in the store's own time zone. A window past
    midnight belongs to the day it starts on."""
    days, hours = window.split()
    first, last = (DAYS.index(d) for d in days.split("-"))
    allowed = {(first + i) % 7 for i in range((last - first) % 7 + 1)}
    start, end = (int(h) * 60 + int(m) for h, m in (part.split(":") for part in hours.split("-")))
    local = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(ZoneInfo(timezone))
    minute = local.hour * 60 + local.minute
    if start < end:
        return local.weekday() in allowed and start <= minute < end
    # Past midnight: the late part today, or the early part of a window that started yesterday.
    return ((local.weekday() in allowed and minute >= start) or
            ((local.weekday() - 1) % 7 in allowed and minute < end))


def judge_prechecks(answer, server, settings, now=None):
    """[(what, ok)] for one server's pre-check answer and its store's window."""
    free = answer.get("free_gb") or 0
    pending = answer.get("pending_reboot") or []
    window = server["maintenance_window"]
    is_open = in_window(window, server["timezone"], now)
    return [
        (f"free disk {free} GB (at least {settings['min_free_gb']})", free >= settings["min_free_gb"]),
        (f"service {answer.get('service', 'unknown')}", answer.get("service") == "Running"),
        ("no pending reboot" if not pending else f"pending reboot: {', '.join(pending)}", not pending),
        (f"maintenance window {window} {server['timezone']}: {'open' if is_open else 'closed'}", is_open),
    ]


def run(waves, upgrade, parallel, on_wave=None):
    """Runs upgrade(name) for every server, wave by wave, never more than
    `parallel` at once. upgrade returns a dict with "failed": True or False.

    Returns (results, stopped_by): results {name: dict} for the servers that
    ran, stopped_by the first server that failed, or None. on_wave(number,
    names, status) is called when a wave starts ("running") and ends ("done"
    or "stopped").
    """
    results, stopped_by = {}, None
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for number, wave in enumerate(waves, 1):
            if stopped_by:
                break
            if on_wave:
                on_wave(number, wave, "running")
            queue, running = list(wave), {}
            while queue or running:
                while queue and len(running) < parallel and not stopped_by:
                    name = queue.pop(0)
                    running[pool.submit(upgrade, name)] = name
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    name = running.pop(future)
                    results[name] = future.result()
                    if results[name].get("failed") and not stopped_by:
                        stopped_by = name
            if on_wave:
                on_wave(number, wave, "stopped" if stopped_by else "done")
    return results, stopped_by


def slack_line(result, target):
    """The line Slack shows when a server stops the wave."""
    name, before = result["server"], result.get("before")
    step = result.get("failed_at")
    if step == "prechecks":
        return f"{name} failed its pre-checks, not touched. Wave stopped."
    if step == "download":
        return f"{name} couldn't get a verified {target} package, still on {before}. Wave stopped."
    if result.get("rollback_ok") is False:
        return f"{name} failed its check, and going back to {before} failed too. Look now. Wave stopped."
    return f"{name} failed its check, back on {before}. Wave stopped."


def duration(seconds):
    """Minutes and seconds, never rounded down: 1m 02s."""
    seconds = int(-(-seconds // 1))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def work_note(result, target):
    """The change's work note for one server: pre-checks, versions before and
    after, checksum, health result and duration."""
    name = result["server"]
    head = {HEALTHY: f"upgraded to {target} and healthy", ROLLED_BACK: f"failed its check, back on {result.get('after')}",
            FAILED: "failed", "skipped": f"already on {target}, skipped"}.get(result["status"], result["status"])
    lines = [f"{name} ({result['store']}), wave {result['wave']}: {head}"]
    if result.get("prechecks"):
        lines.append("Pre-checks: " + "; ".join(f"{what} {'ok' if ok else 'FAILED'}" for what, ok in result["prechecks"]))
    lines.append(f"Versions: {result.get('before')} before, {result.get('after')} after")
    if result.get("sha256"):
        lines.append(f"Package: {result['package']}, SHA-256 {result['sha256']} verified on the server")
    if result.get("health"):
        lines.append(f"Health: {result['health']}")
    if result.get("rollback"):
        lines.append(f"Rollback: {result['rollback']}")
    lines.append(f"Duration: {duration(result['seconds'])}")
    if result.get("commands"):
        lines.append("Run Command: " + ", ".join(result["commands"]))
    return "\n".join(lines)


def summary(results, plan_waves, target, stopped_by):
    """One paragraph on where the fleet stands after a run, for Slack, Jira and the change."""
    servers = [s for wave in plan_waves for s in wave]
    upgraded = [s for s in servers if results.get(s, {}).get("status") in (HEALTHY, "skipped")]
    untouched = [s for s in servers if s not in results]
    text = f"{len(upgraded)} of {len(servers)} servers on {target}."
    if stopped_by:
        failed = results[stopped_by]
        text += f" {stopped_by} failed ({failed.get('reason', 'see its note')}) and is on {failed.get('after')}."
        if untouched:
            text += f" Not started: {', '.join(untouched)}."
    return text


def names(servers):
    """STORE-005, STORE-006 and STORE-007."""
    return servers[0] if len(servers) == 1 else f"{', '.join(servers[:-1])} and {servers[-1]}"


def finish_line(results, plan_waves, others, target, stopped_by):
    """Slack's last line after the stop rule, true to what each server runs:
    the servers not started, the ones that failed besides the one that
    stopped the wave, and the rest on the target. others: the servers outside
    the plan, which run it already. {total} stays for notify.py."""
    servers = [s for wave in plan_waves for s in wave]
    untouched = [s for s in servers if s not in results]
    failed = [s for s in servers if s in results and s != stopped_by
              and results[s].get("status") not in (HEALTHY, "skipped")]
    upgraded = [s for s in [*servers, *others] if s != stopped_by and s not in untouched and s not in failed]
    parts = []
    if untouched:
        parts.append(f"{names(untouched)} {'was' if len(untouched) == 1 else 'were'} not touched")
    if failed:
        parts.append(f"{names(failed)} failed too")
    if upgraded:
        parts.append(f"the others run {target}" if parts else f"The others run {target}")
    line = f"Wave stopped at {stopped_by} after {{total}}."
    return f"{line} {'; '.join(parts)}." if parts else line

