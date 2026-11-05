"""Video 32's right-hand window, on the Mac: calls.py's lines as it prints them on the tools host.

    .venv/bin/python scripts/demos/one_wrong_context/window.py

Through an interactive Session Manager session, as video 25's lines.py, so each
line arrives as it happens. The count stays on the host, where the Service is:
an SSM tunnel from the Mac would count its own failures as the Service's. The
host runs its own checkout, which a reset pulls. When the session ends the window
keeps what it shows, with no prompt, until Ctrl-C.
"""

import json
import signal
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # after the standard library: scripts/ has a secrets.py

import host  # noqa: E402

CALLS = "scripts/demos/one_wrong_context/calls.py"
SESSION_NOISE = ("Starting session with SessionId", "Exiting session with sessionId", "Cannot perform start session")


def command():
    return f"{host.PYTHON} {host.REPO_DIR}/{CALLS}"


def show(line):
    if line.strip() and not line.startswith(SESSION_NOISE):
        print(line, flush=True)


def leave(process):
    """aws hands the session to session-manager-plugin, its child, which holds this window's
    keyboard in cbreak with no echo: left behind, it would eat what's typed here next."""
    subprocess.run(["pkill", "-TERM", "-P", str(process.pid)], capture_output=True)
    process.terminate()
    subprocess.run(["stty", "sane"], stderr=subprocess.DEVNULL)


def stream(the_host, popen=subprocess.Popen):
    """calls.py's lines as they come. True when any came."""
    from common import aws_cli_env

    process = popen(["aws", "ssm", "start-session", "--target", the_host.id, "--document-name",
                     "AWS-StartInteractiveCommand", "--parameters", json.dumps({"command": [command()]})],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=aws_cli_env())
    shown = False
    try:
        for raw in process.stdout:
            show(raw.rstrip("\r\n"))
            shown = True
    finally:
        leave(process)
    return shown


def main():
    try:
        if not stream(host.Host()):
            print("The session gave no lines: is the tools host up, and on a checkout with this demo?", flush=True)
        signal.pause()  # the session ended: hold the screen
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
