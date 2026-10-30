"""Calibrate demo 15's timing test: time it 200 times, as the pipeline runs it, and set its limit.

    python scripts/demos/flaky/calibrate.py               on the tools host, in the toolchain container
    python scripts/demos/flaky/calibrate.py --write       the same, then writes the limit into 1.5.0 and 1.5.1
    python scripts/demos/flaky/calibrate.py --local       here, in this Python, for a first figure

test_delivery_estimate_timeout renders the page for every region a fixed
number of times and asserts it finishes within TIME_LIMIT_SECONDS. Each run
here is the pipeline's first pass: a fresh pytest of the version's test file,
with coverage, in the Python toolchain image. The test records its own time
(the property elapsed_seconds), and this prints the 50th, 90th and 99th
percentiles. The limit goes near the 90th, so about one run in ten misses it,
with the measured figures in a comment beside it.

From the Mac it hands itself over to the tools host through SSM Run Command
(scripts/host.py), like reset.py, and prints the log there.
"""

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
ROOT = SCRIPTS.parent
STATES = ROOT / "demo-states" / "orders-service"
VERSION = "1.5.0"
TEST = "test_delivery_estimate_timeout"
# In the toolchain container this file sits as deep as in the repository, so the
# paths above resolve there too (at /calibrate, HERE.parents[1] doesn't exist).
CONTAINER_SCRIPT = "/release-demo/scripts/demos/flaky/calibrate.py"
# The limit and the figures it came from, the line above it.
LIMIT_LINES = re.compile(r"^# Measured: .*\nTIME_LIMIT_SECONDS = .*$", re.MULTILINE)


def percentile(values, share):
    """Nearest rank: the smallest value with at least share of the values at or below it."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(-(-share * len(ordered) // 1)) - 1))]


def one_run(state_dir):
    """One first pass of the state's test file, as tests_run.py runs it. Returns the test's seconds."""
    with tempfile.TemporaryDirectory() as temp:
        report = Path(temp) / "junit.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
                        f"--junitxml={report}", "--cov=.", "--cov-report=", "tests"],
                       cwd=state_dir, capture_output=True, env={"COVERAGE_FILE": str(Path(temp) / "coverage"),
                                                                "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": temp})
        for case in ET.parse(report).getroot().iter("testcase"):
            if case.get("name") == TEST:
                for prop in case.iter("property"):
                    if prop.get("name") == "elapsed_seconds":
                        return float(prop.get("value"))
    raise RuntimeError(f"{TEST} recorded no elapsed_seconds: is {state_dir} the right demo state?")


def inside(state_dir, runs):
    """The loop, in whatever Python runs this: prints one JSON list of seconds."""
    times = []
    for index in range(runs):
        times.append(one_run(state_dir))
        if (index + 1) % 20 == 0:
            print(f"  {index + 1} runs", file=sys.stderr, flush=True)
    print(json.dumps(times))


def toolchain_image():
    """The Python toolchain image, tagged as the pipeline tags it (jenkins/shared-library/vars/toolchain.groovy)."""
    dockerfile = ROOT / "jenkins" / "toolchain" / "python.Dockerfile"
    digest = hashlib.sha256(dockerfile.read_bytes() + (ROOT / "requirements-dev.txt").read_bytes()).hexdigest()[:12]
    tag = f"release-demo/python-tools:{digest}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        print(f"Building {tag}: the pipeline hasn't yet on this host.", flush=True)
        subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(dockerfile), str(ROOT)], check=True)
    return tag


def in_container(state_dir, runs):
    tag = toolchain_image()
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp) / "orders-service"
        subprocess.run(["cp", "-R", str(state_dir), str(work)], check=True)
        subprocess.run(["chmod", "-R", "a+rwX", temp], check=True)
        result = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/work", "-v",
                                 f"{HERE}:{CONTAINER_SCRIPT.rsplit('/', 1)[0]}:ro", "-w", "/work", "-e", "HOME=/tmp",
                                 tag, "python", CONTAINER_SCRIPT, "--inside", "/work", "--runs", str(runs)],
                                stdout=subprocess.PIPE, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def limit_lines(times, where):
    """The figures, and the limit near the 90th percentile, as the test file holds them."""
    p50, p90, p99 = (percentile(times, share) for share in (0.5, 0.9, 0.99))
    today = datetime.date.today()
    return (f"# Measured: p50 {p50:.3f} s, p90 {p90:.3f} s, p99 {p99:.3f} s in {len(times)} runs on {where}, "
            f"{today.day} {today:%b %Y}.\nTIME_LIMIT_SECONDS = {p90:.3f}")


def write(lines):
    for version in ("1.5.0", "1.5.1"):
        path = STATES / version / "tests" / "test_handler.py"
        text = path.read_text()
        if not LIMIT_LINES.search(text):
            raise SystemExit(f"{path} has no '# Measured:' and TIME_LIMIT_SECONDS lines to replace.")
        path.write_text(LIMIT_LINES.sub(lambda _: lines, text, count=1))
        print(f"  {path.relative_to(ROOT)}: written")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--write", action="store_true", help="write the limit into demo states 1.5.0 and 1.5.1")
    parser.add_argument("--local", action="store_true", help="run here, without the toolchain container")
    parser.add_argument("--here", action="store_true", help="run on this machine (the tools host), not hand over")
    parser.add_argument("--inside", metavar="STATE", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.inside:
        inside(Path(args.inside), args.runs)
        return 0
    state_dir = STATES / VERSION
    if args.local:
        print(f"Timing {TEST} {args.runs} times here, with coverage, in {sys.executable}.", flush=True)
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "orders-service"
            subprocess.run(["cp", "-R", str(state_dir), str(work)], check=True)
            times = [one_run(work) for _ in range(args.runs)]
        where = "a Mac" if sys.platform == "darwin" else "this machine"
    else:
        sys.path.insert(0, str(SCRIPTS))
        import host

        if not args.here and not host.on_host():
            code = host.hand_over("calibrate", "demos/flaky/calibrate.py",
                                  [f"--runs={args.runs}"], title=f"calibrate.py {args.runs} runs")
            if code == 0 and args.write:
                # The host printed the line; write it here, into this checkout, to commit.
                _, out, _ = host.Host().shell("grep -A1 '^# Measured: ' /var/log/release-demo-calibrate.log | tail -2")
                if not out.strip().endswith(tuple("0123456789")) or "TIME_LIMIT_SECONDS = " not in out:
                    raise SystemExit("The tools host's log has no measured limit: see its log.")
                write(out.strip())
            return code
        print(f"Timing {TEST} {args.runs} times in the Python toolchain container, with coverage.", flush=True)
        times = in_container(state_dir, args.runs)
        where = "the tools host"
    lines = limit_lines(times, where)
    limit = float(lines.rsplit(" ", 1)[1])
    misses = sum(1 for seconds in times if seconds >= limit)
    print(lines)
    print(f"With that limit, {misses} of {len(times)} runs here would have failed the test.")
    if args.write:
        write(lines)
    else:
        print("Set it in both demo states with --write, or by hand, then commit them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
