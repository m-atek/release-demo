"""Video 25's bottom window: the Blip check's lines as it prints them, on the Mac.

    .venv/bin/python scripts/demos/database-blip/lines.py          from the log's current end
    .venv/bin/python scripts/demos/database-blip/lines.py --poll   asking every second, if the session lags

Through an interactive Session Manager session on follow_log.py, as rotate.py
does, so each line arrives as it happens. The lines are the check's own, with
colours only: 200 green, 503 amber, refused and timeout red. When the session
ends the window keeps what it shows, with no prompt, until Ctrl-C. Restart it
before each take.
"""

import argparse
import json
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

import host  # noqa: E402

LOG = "/var/tmp/release-demo-blip.log"
SESSION_NOISE = ("Starting session with SessionId", "Exiting session with sessionId", "Cannot perform start session")
RESET = "\033[0m"
COLOURS = {"2": "\033[32m", "5": "\033[38;5;214m", "r": "\033[31m", "t": "\033[31m"}
# A result in a second's line: "200 x10", "503 x4", "refused x6", or the short "ref:2" and "tim:1".
RESULT = re.compile(r"(?<![\w:/.-])(200|503|refused|timeout|ref|tim)(?: x\d+|:\d+)")
SECOND = re.compile(r"^-?\d+:\d\d +[\w-]+[ :x]")


def coloured(line):
    """The line as printed, with its results in colour. Only a second's line has any."""
    if not SECOND.match(line) or line.endswith(("database blocked", "database back")) or "pods" in line or "failed" in line:
        return line
    return RESULT.sub(lambda m: f"{COLOURS[m.group(1)[0]]}{m.group(0)}{RESET}", line)


def show(line):
    if not (line.startswith("== ") or line.startswith(SESSION_NOISE) or not line.strip()):
        print(coloured(line), flush=True)


def log_size(the_host):
    code, out, err = the_host.shell(f"stat -c %s {LOG} 2>/dev/null || echo 0")
    if code != 0:
        raise SystemExit(f"The tools host didn't answer: {err.strip()[:200]}")
    return int(out.strip().splitlines()[-1])


def stream(the_host, offset, popen=subprocess.Popen):
    """The log from offset as it grows, through Session Manager. False when the session never ran."""
    from common import aws_cli_env

    command = f"{host.PYTHON} {host.REPO_DIR}/scripts/follow_log.py {LOG} {offset}"
    process = popen(["aws", "ssm", "start-session", "--target", the_host.id, "--document-name",
                     "AWS-StartInteractiveCommand", "--parameters", json.dumps({"command": [command]})],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=aws_cli_env())
    shown = False
    try:
        for raw in process.stdout:
            show(raw.rstrip("\r\n"))
            shown = True
    finally:
        process.terminate()
    return shown


def poll(the_host, offset, interval=1):
    """The fallback: ask for the log's new bytes every second, as rotate.py does without a session."""
    while True:
        code, out, _ = the_host.shell(f"tail -c +{offset + 1} {shlex.quote(LOG)} 2>/dev/null", timeout=30)
        if code == 0 and out:
            complete = out[:out.rfind("\n") + 1]
            for line in complete.splitlines():
                show(line)
            offset += len(complete.encode())
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--poll", action="store_true", help="ask every second instead of holding a session")
    args = parser.parse_args()
    the_host = host.Host()
    offset = log_size(the_host)
    try:
        if args.poll:
            poll(the_host, offset)
        elif not stream(the_host, offset):
            poll(the_host, offset)
        signal.pause()  # the session ended, after follow_log's 15 minutes: hold the screen
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
