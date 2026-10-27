"""Put a prepared version of a service on main.

    python scripts/stage.py orders-service 1.4.0

Copies demo-states/<service>/<version>/ over the service folder and pushes one
commit, "demo: stage orders-service 1.4.0 [skip ci]". This is how a take or a
call gets the code it needs. --no-commit only copies, for local development.
"""

import argparse
import shutil

from common import ROOT, fail, git, service, version_of


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("version")
    parser.add_argument("--no-commit", action="store_true", help="copy only, no commit or push")
    parser.add_argument("--no-push", action="store_true", help="commit, but don't push")
    args = parser.parse_args()

    config = service(args.service)
    source = ROOT / "demo-states" / args.service / args.version
    if not source.is_dir():
        states = sorted(p.name for p in source.parent.iterdir() if p.is_dir())
        fail(f"No demo state {args.version} for {args.service}. Available: {', '.join(states)}")
    if version_of(source) != args.version:
        fail(f"{source}/VERSION says {version_of(source)}, expected {args.version}")

    target = config["dir"]
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    print(f"Staged {args.service} {args.version} in {config['path']}")

    if args.no_commit:
        return

    path = config["path"]
    git("add", "--all", "--", path)
    if git("diff", "--cached", "--quiet", "--", path, check=False).returncode == 0:
        print("Nothing changed: main already has this version")
        return
    git("commit", "--quiet", "-m", f"demo: stage {args.service} {args.version} [skip ci]", "--", path)
    print(f"Committed {git('rev-parse', '--short', 'HEAD').stdout.strip()}")

    if args.no_push or not git("remote", check=False).stdout.split():
        print("Not pushed")
        return
    git("push", "--quiet", "origin", "HEAD:main")
    print("Pushed to main")


if __name__ == "__main__":
    main()
