"""Wait for a quiet tools host before one of flaky-night's runs (demo 15).

    python3 scripts/demos/flaky/quiet.py               wait until the host is quiet, then go on
    python3 scripts/demos/flaky/quiet.py --report 6    print six samples, wait for nothing

calibrate.py measured the timing test's limit while nothing else ran on the
tools host. A night run that times it while another build keeps the CPUs busy
(the ci job after every push to main, a release, another demo's script) runs
it about a third slower and fails it twice, and the dashboard would count that
as a real failure for 14 days. So each run that flaky-night starts first waits
here, before its Build stage, until no other Jenkins build runs (the timer job
pull-requests aside) and the host's CPUs, all of them together, are less than
--busy busy in two samples of --seconds in a row. After --wait-minutes it goes
on anyway and says so. If Jenkins doesn't answer, the CPUs decide alone.
"""

import argparse
import sys
import time
from pathlib import Path

STAT = Path("/proc/stat")
# Builds that aren't another build on the host: this run, the night that
# started it, and the timer job that mostly ends at once (as tools-host/idle_stop.py).
OURS = ("/job/orders-service-tests/", "/job/flaky-night/", "/job/pull-requests/")


def cpu_times(text):
    """(busy, total) jiffies from /proc/stat's first line. Guest time is already
    in user and nice; idle and iowait are the only time no CPU worked."""
    fields = [int(value) for value in text.splitlines()[0].split()[1:9]]
    user, nice, system, idle, iowait, irq, softirq, steal = fields
    total = sum(fields)
    return total - idle - iowait, total


def busy_share(read=STAT.read_text, sleep=time.sleep, seconds=10):
    """The share of all CPUs' time spent working over the next seconds, 0 to 1."""
    busy_before, total_before = cpu_times(read())
    sleep(seconds)
    busy_after, total_after = cpu_times(read())
    elapsed = total_after - total_before
    return (busy_after - busy_before) / elapsed if elapsed > 0 else 0.0


def other_builds():
    """The Jenkins builds running besides ours, by name, such as ['ci #82'], or None if Jenkins can't be asked."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from release_job import ReleaseJob

        computers = ReleaseJob.as_release_bot().get(
            "/computer/api/json?tree=computer[executors[currentExecutable[url,fullDisplayName]],"
            "oneOffExecutors[currentExecutable[url,fullDisplayName]]]")["computer"]
    except (Exception, SystemExit):
        return None
    running = [e["currentExecutable"] for c in computers for e in (c.get("executors") or []) +
               (c.get("oneOffExecutors") or []) if e.get("currentExecutable")]
    return sorted({build.get("fullDisplayName") or build.get("url", "") for build in running
                   if not any(part in (build.get("url") or "") for part in OURS)})


def wait_until_quiet(busy=0.2, seconds=10, wait_minutes=45, builds=other_builds, read=STAT.read_text,
                     sleep=time.sleep, clock=time.monotonic, out=print):
    """Returns (seconds waited, quiet at the end). Two quiet samples in a row end the wait."""
    started = clock()
    quiet_in_a_row, shares, seen = 0, [], set()
    while True:
        running = builds()
        share = busy_share(read, sleep, seconds)
        shares.append(share)
        seen.update(running or [])
        quiet_in_a_row = quiet_in_a_row + 1 if share < busy and not running else 0
        waited = clock() - started
        if quiet_in_a_row >= 2:
            if waited > 2 * seconds + 1:
                because = f" while {', '.join(sorted(seen))} ran" if seen else ""
                out(f"Waited {waited / 60:.1f} minutes for a quiet host{because}: its CPUs were up to "
                    f"{max(shares):.0%} busy, now {share:.0%}.")
            else:
                out(f"The host is quiet: its CPUs are {share:.0%} busy{'' if running is not None else ' (Jenkins not asked)'}.")
            return waited, True
        if waited >= wait_minutes * 60:
            out(f"Going on after {wait_minutes} minutes: the host stayed busy ({share:.0%} of its CPUs"
                f"{', ' + ', '.join(running) if running else ''}). This run's timing test may fail for that reason.")
            return waited, False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--busy", type=float, default=0.2, help="the busy share of all CPUs that counts as quiet")
    parser.add_argument("--seconds", type=int, default=10, help="the length of one sample")
    parser.add_argument("--wait-minutes", type=int, default=45)
    parser.add_argument("--report", type=int, metavar="SAMPLES", help="print this many samples and stop")
    args = parser.parse_args()
    if args.report:
        for _ in range(args.report):
            print(f"{time.strftime('%H:%M:%S')} {busy_share(seconds=args.seconds):.1%} busy, "
                  f"other builds: {other_builds()}", flush=True)
        return 0
    wait_until_quiet(args.busy, args.seconds, args.wait_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
