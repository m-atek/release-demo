"""Stages 6 and 10 for the store fleet: store-sync upgraded in waves through
SSM Run Command, with no logins and no open ports.

    python scripts/demos/windows/deploy.py store-sync staging                the plan only, by hand (the fleet has no staging)
    python scripts/demos/windows/deploy.py store-sync production             the waves
    python scripts/demos/windows/deploy.py store-sync production --back      this run's servers back where they were
    python scripts/demos/windows/deploy.py store-sync production --previous  every server to the release before (/rollback)

staging: the fleet has no staging environment; the first wave, one server
alone, is the canary. The pipeline skips Staging and plans in its Plan stage
(plan.py); this step does the same by hand. It reads what every server runs
and plans the waves from services.yaml: all servers, or with the option
resume only those not on the new version. The tiles in Grafana show the plan,
and the run state gets change_extras (plan.py) for the change Alex approves.

production, per server in its wave, never more than `parallel` at once:
1. pre-checks: free disk, the service running, no pending reboot, and the
   store's maintenance window;
2. install: the server downloads the exact published package from JCR and
   verifies its SHA-256 against the run state, before anything stops; then
   stop, switch to the new version's folder, start;
3. check: /health must report the new version within 60 seconds;
4. after a failed check: switch back to the version before, start, check it.

The stop rule: when a server fails, the servers already running finish, no
new one starts, and the change closes as "Successful with issues". Slack gets
"STORE-004 failed its check, back on 3.2.0. Wave stopped." and the run's
finish line, and the script exits 1 so the pipeline stops.

Every server gets a work note in the change (pre-checks, versions before and
after, checksum, health result, duration), a Splunk event, and a Grafana tile
per status. The Slack message's wave rows are in the run state as "rows".
For a run by hand without Slack, set RELEASE_DEMO_SLACK=off.
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import fleet_events  # noqa: E402
import plan as plan_change  # noqa: E402
import run_state  # noqa: E402
import waves  # noqa: E402
from common import ROOT, fail, service  # noqa: E402
from fleet import Fleet, describe_failure  # noqa: E402

NUMBERS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


class Release:
    """One run's view of the fleet: the run state, the change and Slack, shared by the server threads."""

    def __init__(self, config, state, fleet=None):
        self.config, self.state = config, state
        self.env = config["environments"]["production"]
        self.fleet = fleet or Fleet(config)
        self.target = state["version"]
        self.lock = threading.Lock()
        self.sn = None
        if state.get("change"):
            import change
            self.change = change
            self.sn, _ = change.connect()

    @property
    def change_number(self):
        return self.state.get("change", {}).get("number")

    def comment(self, what, name):
        suffix = f" ({self.change_number})" if self.change_number else ""
        return f"store-sync {self.target}: {what} {name}{suffix}"

    def save(self):
        with self.lock:
            run_state.save(self.state)

    def note(self, text):
        if self.sn:
            self.change.note(self.sn, self.state, text)

    def notify(self, *args):
        """notify.py, the shared Slack writer. It reads and writes the run state itself."""
        if os.environ.get("RELEASE_DEMO_SLACK") == "off":
            print(f"Slack (off): {' '.join(args)}")
            return
        with self.lock:
            run_state.save(self.state)
            subprocess.run([sys.executable, str(ROOT / "scripts" / "notify.py"), *args], check=False)
            fresh = run_state.load()
            self.state.clear()
            self.state.update(fresh)

    def tile(self, name, status, version):
        servers = self.state["plan"]["servers"]
        with self.lock:
            servers[name].update({"status": status, "version": version})
            self.state["rows"] = rows(self.state["plan"])
        fleet_events.tile(self.state["service"], name, status, version, servers[name]["wave"], self.change_number)

    def progress(self, label):
        servers = self.state["plan"]["servers"]
        everyone = {**{n: {"version": v} for n, v in self.state["plan"].get("others", {}).items()}, **servers}
        done = sum(1 for s in everyone.values() if s["version"] == self.target)
        fleet_events.progress(self.state["service"], label, done, len(everyone), self.target)


def rows(plan):
    """The Slack message's wave rows: one per wave, each server with its version and status.
    Wave 3  STORE-004 3.2.0 rolled back · STORE-005 3.3.0 healthy"""
    lines = []
    for number, wave in zip(plan["numbers"], plan["waves"]):
        cells = [f"{name} {plan['servers'][name]['version']} {plan['servers'][name]['status']}" for name in wave]
        lines.append(f"Wave {number}  " + " · ".join(cells))
    return lines


def make_plan(release, ids):
    """Reads what every server runs, then plans the waves."""
    state, env, target = release.state, release.env, release.target
    answers = release.fleet.state(ids, f"store-sync {target}: read the fleet before the plan")
    unreachable = [name for name, answer in answers.items() if not answer.ok]
    if unreachable:
        fail(f"No answer through Run Command from {', '.join(unreachable)}.")
    versions = {name: answer.result.get("version") for name, answer in answers.items()}
    resume = state.get("option") == "resume"
    planned = waves.plan(env["waves"], versions, target, resume)
    if not planned:
        fail(f"Every server already runs {target}: nothing to resume.")
    names = [name for wave in planned for name in wave]
    # A resume keeps each server's wave number from services.yaml.
    numbers = [next(i for i, w in enumerate(env["waves"], 1) if set(p) <= set(w)) for p in planned]
    state["plan"] = {
        "waves": planned, "numbers": numbers, "total_waves": len(env["waves"]), "parallel": env["parallel"],
        "resume": resume,
        "servers": {name: {"store": env["servers"][name]["store"], "wave": number, "before": versions[name],
                           "version": versions[name], "status": waves.WAITING}
                    for number, wave in zip(numbers, planned) for name in wave},
        "others": {name: v for name, v in versions.items() if name not in names},
    }
    state["rows"] = rows(state["plan"])
    if not state.get("change"):
        import change
        sn, _ = change.connect()
        state["change_extras"] = plan_change.change_extras(sn, release.config, state, state["plan"])
    release.save()
    for name in names:
        release.tile(name, waves.WAITING, versions[name])
    release.progress(f"{len(planned)} {'wave' if len(planned) == 1 else 'waves'} planned")
    for number, wave in zip(numbers, planned):
        print(f"Wave {number}: {', '.join(f'{n} (on {versions[n]})' for n in wave)}")
    print(f"Never more than {env['parallel']} at once. {'Resume: only the servers not on ' + target if resume else ''}".strip())
    return state["plan"]


def upgrade(release, name, instance_id, package, jcr_url):
    """One server, from pre-checks to its check, and back if the check fails."""
    env, target, server = release.env, release.target, release.state["plan"]["servers"][name]
    ids = {name: instance_id}
    started = time.monotonic()
    result = {"server": name, "store": server["store"], "wave": server["wave"], "before": server["before"],
              "after": server["before"], "commands": [], "failed": False, "package": package["path"]}

    def finish(status, **extra):
        result.update(status=status, seconds=time.monotonic() - started, **extra)
        release.tile(name, status if status != "skipped" else waves.HEALTHY, result["after"])
        release.note(waves.work_note(result, target))
        fleet_events.to_splunk(release.state, result)
        if result["failed"]:
            release.notify("line", waves.slack_line(result, target))
        else:
            release.notify("stage", "Production", "running")
        print(f"{name}: {status}, {result['before']} before, {result['after']} after, {waves.duration(result['seconds'])}")
        return result

    release.tile(name, waves.UPGRADING, server["before"])
    pre = release.fleet.run(ids, "prechecks", release.comment("pre-checks on", name), timeout=120)[name]
    result["commands"].append(pre.command_id)
    if not pre.ok:
        return finish(waves.FAILED, failed=True, failed_at="prechecks", reason=describe_failure(pre))
    result["before"] = result["after"] = pre.result.get("version")
    if result["before"] == target:
        return finish("skipped")
    result["prechecks"] = waves.judge_prechecks(pre.result, env["servers"][name], env["prechecks"])
    if not all(ok for _, ok in result["prechecks"]):
        failed = "; ".join(what for what, ok in result["prechecks"] if not ok)
        return finish(waves.FAILED, failed=True, failed_at="prechecks", reason=f"pre-checks: {failed}")

    install = release.fleet.run(ids, "install", release.comment("install on", name), timeout=300, JcrUrl=jcr_url,
                                Version=target, PackagePath=package["path"], Sha256=package["sha256"])[name]
    result["commands"].append(install.command_id)
    result["sha256"] = install.result.get("sha256")
    if not install.ok or not install.result.get("ok"):
        if install.result.get("step") == "download" or not install.ok:
            result.pop("sha256", None)
            return finish(waves.FAILED, failed=True, failed_at="download", reason=describe_failure(install),
                          after=install.result.get("after", result["before"]))
        health = None
        result["health"] = f"didn't start: {describe_failure(install)}"
    else:
        health = release.fleet.run(ids, "health", release.comment("health check on", name), timeout=120,
                                   Expected=target, TimeoutSeconds=env["check"]["timeout_seconds"])[name]
        result["commands"].append(health.command_id)
        result["health"] = health_text(health.result, target)
        if health.ok and health.result.get("ok"):
            return finish(waves.HEALTHY, after=target)

    # The check failed: back to the version before, started and checked.
    release.tile(name, waves.FAILED, target)
    back = release.fleet.run(ids, "switch", release.comment(f"roll back to {result['before']} on", name), timeout=180,
                             Target=result["before"], TimeoutSeconds=env["check"]["timeout_seconds"])[name]
    result["commands"].append(back.command_id)
    rollback_ok = back.ok and back.result.get("ok")
    result["rollback"] = (f"{result['before']} back and healthy after {back.result.get('seconds')}s" if rollback_ok
                          else f"FAILED: {describe_failure(back)}")
    reason = "; ".join((health.result.get("problems") or []) if health else []) or result["health"]
    return finish(waves.ROLLED_BACK if rollback_ok else waves.FAILED, failed=True, failed_at="check", reason=reason,
                  after=back.result.get("after") or target, rollback_ok=rollback_ok)


def health_text(answer, target):
    if answer.get("ok"):
        return f"/health reports {target} after {answer.get('seconds')}s"
    seen = f"answered {answer.get('code')} with {answer.get('version')}" if answer.get("code") else "did not answer"
    problems = "; ".join(answer.get("problems") or [])
    return f"/health {seen} after {answer.get('seconds')}s, expected {target}" + (f": {problems}" if problems else "")


def stopped(release, results, stopped_by):
    """The stop rule's record: the change closes as successful with issues, Slack says where."""
    state, target = release.state, release.target
    plan = state["plan"]
    text = waves.summary(results, plan["waves"], target, stopped_by)
    state["finish_line"] = waves.finish_line(results, plan["waves"], list(plan.get("others", {})), target, stopped_by)
    release.save()
    if release.sn:
        took = run_state.duration(run_state.now() - state["started_at"])
        release.change.close(release.sn, state, "successful_issues",
                             f"{text} The wave stopped at {stopped_by}; the servers not on {target} are upgraded "
                             f"with /release {state['service']} {state['jira_key']} resume. Total time: {took}. "
                             f"Build: {state['build'].get('url', '')}")
        state["change"]["closed"] = "successful_issues"
        release.save()
    print(text)


def production(release):
    state, env = release.state, release.env
    if not state.get("package"):
        fail("No package in the run state: publish first.")
    ids = release.fleet.running()
    if not state.get("plan"):
        make_plan(release, ids)
    plan = state["plan"]
    jcr_url = release.fleet.jcr_url()
    package = state["package"]
    resumes = state.get("change_extras", {}).get("resumes")
    if resumes and release.sn:
        release.change.note(release.sn, {"change": resumes},
                            f"Resumed in {release.change_number}: {', '.join(n for w in plan['waves'] for n in w)}.")
    deploys = state.setdefault("deploys", {})
    befores = [s["before"] for s in plan["servers"].values()]
    deploys["production"] = {"number": release.target, "previous_number": max(set(befores), key=befores.count),
                             "previous_version": max(set(befores), key=befores.count), "url": "",
                             "deployed_at": run_state.now(), "servers": {}}
    release.save()

    def on_wave(index, names, status):
        release.progress(f"Wave {plan['numbers'][index - 1]} of {plan['total_waves']}: {status}")

    results, stopped_by = waves.run(plan["waves"], lambda name: upgrade(release, name, ids[name], package, jcr_url),
                                    plan["parallel"], on_wave)
    deploys = state.setdefault("deploys", {})
    deploys["production"]["servers"] = {name: {k: r.get(k) for k in ("status", "before", "after", "seconds")}
                                        for name, r in results.items()}
    deploys["production"]["result"] = "partial" if stopped_by else "complete"
    release.save()
    if stopped_by:
        stopped(release, results, stopped_by)
        sys.exit(1)
    print(waves.summary(results, plan["waves"], release.target, None))


def back(release, previous):
    """--back: this run's upgraded servers to where they were. --previous: every
    server to the newest installed version below the one it runs."""
    state, env = release.state, release.env
    ids = release.fleet.running()
    targets = {}
    if previous:
        answers = release.fleet.state(ids, f"store-sync: read the fleet before the rollback")
        for name, answer in answers.items():
            current = answer.result.get("version")
            older = [v for v in answer.result.get("installed", []) if _key(v) < _key(current or "0.0.0")]
            if older:
                targets[name] = max(older, key=_key)
    else:
        for name, server in state.get("deploys", {}).get("production", {}).get("servers", {}).items():
            if server.get("after") != server.get("before"):
                targets[name] = server["before"]
    if not targets:
        print("Nothing to roll back.")
        return
    results = {}
    for name, version in targets.items():
        results[name] = release.fleet.run({name: ids[name]}, "switch",
                                          f"store-sync: roll back {name} to {version}", timeout=180,
                                          Target=version, TimeoutSeconds=env["check"]["timeout_seconds"])[name]
        ok = results[name].ok and results[name].result.get("ok")
        print(f"{name}: {'back on ' + version if ok else 'FAILED: ' + describe_failure(results[name])}")
        fleet_events.tile(state["service"], name, waves.ROLLED_BACK if ok else waves.FAILED, version)
    deploys = state.setdefault("deploys", {}).setdefault("production", {})
    common = sorted(set(targets.values()), key=_key)[0]
    if previous:
        deploys.update({"previous_version": state.get("version"), "previous_number": state.get("version")})
    deploys.update({"rolled_back_to": common, "rolled_back_number": common, "url": "",
                    "rolled_back_servers": sorted(targets)})
    run_state.save(state)
    if not all(r.ok and r.result.get("ok") for r in results.values()):
        sys.exit(1)


def _key(version):
    return tuple(int(part) for part in version.split("."))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment", choices=["staging", "production"])
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--back", action="store_true", help="this run's servers back where they were")
    which.add_argument("--previous", action="store_true", help="every server to the release before")
    args = parser.parse_args()

    config = service(args.service)
    state = run_state.load()
    if not state and (args.back or args.previous):
        state = {"service": args.service, "version": None, "jira_key": "", "build": {"number": "manual"}}
    release = Release(config, state)

    if args.environment == "staging":
        if args.back or args.previous:
            print("No staging to roll back: the plan changed nothing.")
            return
        make_plan(release, release.fleet.running())
        state.setdefault("deploys", {})["staging"] = {
            "number": state["version"], "previous_number": "", "previous_version": "", "url": "",
            "deployed_at": run_state.now(), "plan_only": True}
        release.save()
        return
    if args.back or args.previous:
        back(release, args.previous)
        return
    production(release)


if __name__ == "__main__":
    main()
