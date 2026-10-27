"""Run a service's staging and production side by side on localhost.

    python scripts/dev.py orders-service                  # the working copy, reloads on save
    python scripts/dev.py orders-service --state 1.4.1    # a demo state, without staging it
    python scripts/dev.py orders-service --package dist/orders-service/orders-service-1.4.1.zip

Each environment gets only the settings services.yaml gives it, as its Lambda
function does, not your shell's environment. So configuration drift between
staging and production shows up here before it shows up in AWS.
"""

import argparse
import datetime
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from common import ROOT, fail, http_get, service

WATCHED = ("*.py", "VERSION", "*.json")


def source_dir(config, args):
    if args.package:
        target = ROOT / ".local" / config["name"] / "package"
        shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(args.package) as archive:
            archive.extractall(target)
        return target, False
    if args.state:
        target = ROOT / "demo-states" / config["name"] / args.state
        if not target.is_dir():
            fail(f"No demo state {args.state} in demo-states/{config['name']}/")
        return target, True
    if not (config["dir"] / "VERSION").exists():
        fail(f"{config['path']} is empty. Stage a version first: python scripts/stage.py {config['name']} 1.3.0 --no-commit")
    return config["dir"], True


def snapshot(directory):
    state = {}
    for pattern in WATCHED:
        for path in directory.rglob(pattern):
            if "tests" in path.relative_to(directory).parts:
                continue
            try:
                state[path] = path.stat().st_mtime
            except FileNotFoundError:
                pass
    return state


def settled(directory):
    """Wait until files stop changing, so a restage or a save lands whole."""
    while True:
        before = snapshot(directory)
        time.sleep(0.5)
        after = snapshot(directory)
        if before == after and (directory / "VERSION").exists():
            return after


def start(config, directory):
    deployed_at = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    processes = {}
    for name, env in config["environments"].items():
        environment = {
            "PATH": os.environ["PATH"],
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": name,
            "DEPLOYED_AT": deployed_at,
            **{key: str(value) for key, value in (env.get("settings") or {}).items()},
        }
        processes[name] = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "lambda_local.py"), "--dir", str(directory),
             "--port", str(env["local_port"]), "--handler", config["handler"], "--name", name],
            env=environment,
        )
    return processes


def wait_until_up(config, processes):
    """True when every environment answers. Prints each one's URL and settings."""
    up = True
    for name, env in config["environments"].items():
        url = f"http://127.0.0.1:{env['local_port']}"
        for _ in range(50):
            if processes[name].poll() is not None:
                print(f"  {name:<11} stopped at start: see the error above, or is port {env['local_port']} taken?")
                up = False
                break
            status, _ = http_get(f"{url}/health", timeout=1)
            if status:
                print(f"  {name:<11} {url}   ({', '.join(env.get('settings') or {}) or 'no settings'})")
                break
            time.sleep(0.1)
    return up


def stop(processes):
    for process in processes.values():
        process.terminate()
    for process in processes.values():
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service", nargs="?", default="orders-service")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--state", help="serve demo-states/<service>/<state>/")
    source.add_argument("--package", type=Path, help="serve a zip from scripts/build.py")
    args = parser.parse_args()

    # Stop the environments on kill as well as on Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.stdout.reconfigure(line_buffering=True)

    config = service(args.service)
    directory, reload = source_dir(config, args)
    version = (directory / "VERSION").read_text().strip()
    print(f"{config['name']} {version} from {directory.relative_to(ROOT)}")

    processes = start(config, directory)
    if not wait_until_up(config, processes):
        stop(processes)
        sys.exit(1)
    print("Reloads on save. Ctrl-C stops." if reload else "Ctrl-C stops.")

    seen = snapshot(directory)
    try:
        while True:
            time.sleep(1)
            if reload and snapshot(directory) != seen:
                seen = settled(directory)
                stop(processes)
                processes = start(config, directory)
                version = (directory / "VERSION").read_text().strip()
                print(f"Reloaded {config['name']} {version}")
                wait_until_up(config, processes)
    except KeyboardInterrupt:
        pass
    finally:
        stop(processes)


if __name__ == "__main__":
    main()
