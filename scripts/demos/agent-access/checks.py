"""Acceptance checks 1 to 5 of video 9, inside a fresh coding-agent container.

    python3 /checks/checks.py --account <id> --allowed-env NAME,NAME,...

Run by check.sh (agent_access.py check), never by hand. One line per check, ok or
FAIL, and no model call. Every call on production is one that changes
nothing even if AWS were to allow it: a dry run, a version or alias call
with a revision that can't match, an invoke of type DryRun. Values are never
printed: environment variables by name only, and the SSM call asks for the
parameter's name alone.
"""

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROLE_SESSION = "assumed-role/release-demo-coding-agent/coding-agent"
EXPLICIT_DENY = "with an explicit deny in an identity-based policy"
STAGING, PRODUCTION = "orders-service-staging", "orders-service-production"
NO_MATCH = "0" * 64
NO_REVISION = "00000000-0000-0000-0000-000000000000"


def aws(*args):
    result = subprocess.run(["aws", *args], capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def line(ok, text):
    print(f"{'ok  ' if ok else 'FAIL'} {text}")
    return ok


def denied(stderr, code="AccessDeniedException"):
    return f"({code})" in stderr and EXPLICIT_DENY in stderr


def check_identity():
    code, out, _ = aws("sts", "get-caller-identity", "--query", "Arn", "--output", "text")
    return line(code == 0 and ROLE_SESSION in out, f"1. sts get-caller-identity names {ROLE_SESSION}")


def check_staging_logs():
    code, out, err = aws("logs", "filter-log-events", "--log-group-name", f"/aws/lambda/{STAGING}",
                         "--limit", "5", "--query", "length(events)", "--output", "text")
    return line(code == 0, f"2. reads {STAGING}'s latest log events"
                           f"{f' ({out.strip()} read)' if code == 0 else ': ' + err.strip()[-120:]}")


def check_production(scratch):
    archive = scratch / "check.zip"
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr("handler.py", "")
    calls = {
        "update-function-code": ["lambda", "update-function-code", "--function-name", PRODUCTION,
                                 "--zip-file", f"fileb://{archive}", "--dry-run"],
        "publish-version": ["lambda", "publish-version", "--function-name", PRODUCTION, "--code-sha256", NO_MATCH],
        "update-alias": ["lambda", "update-alias", "--function-name", PRODUCTION, "--name", "live",
                         "--function-version", "1", "--revision-id", NO_REVISION],
        "get-function-configuration": ["lambda", "get-function-configuration", "--function-name", PRODUCTION,
                                       "--query", "FunctionName"],
        "invoke": ["lambda", "invoke", "--function-name", PRODUCTION, "--invocation-type", "DryRun",
                   str(scratch / "invoke.json")],
    }
    results = []
    for name, args in calls.items():
        code, _, err = aws(*args)
        results.append(line(code != 0 and denied(err),
                            f"3. {name} on {PRODUCTION}: "
                            f"{'AccessDeniedException, explicit deny' if denied(err) else err.strip()[-120:] or 'allowed'}"))
    return all(results)


def check_platform(account):
    code, _, err = aws("sts", "assume-role", "--role-arn", f"arn:aws:iam::{account}:role/release-demo-deploy-orders-service",
                       "--role-session-name", "check", "--query", "AssumedRoleUser.Arn")
    first = line(code != 0 and "(AccessDenied)" in err,
                 "4. sts assume-role release-demo-deploy-orders-service: "
                 f"{'AccessDenied' if '(AccessDenied)' in err else 'not refused'}")
    code, _, err = aws("ssm", "get-parameter", "--name", "/release-demo/jcr/token", "--query", "Parameter.Name")
    second = line(code != 0 and "(AccessDeniedException)" in err,
                  "4. ssm get-parameter /release-demo/jcr/token: "
                  f"{'AccessDeniedException' if '(AccessDeniedException)' in err else 'not refused'}")
    return first and second


def credential_files():
    """What the container holds before anything runs in it. Taken first, because
    AWS CLI v2 makes ~/.aws/cli/cache/session.db itself on its first call."""
    home = Path.home()
    found = [str(path) for path in (home / ".aws", home / ".ssh", home / ".git-credentials", home / ".config" / "gh",
                                    home / ".docker", Path("/root/.aws"))
             if os.path.exists(path)]
    gitconfig = home / ".gitconfig"
    if gitconfig.exists() and "credential" in gitconfig.read_text():
        found.append(f"{gitconfig} (a credential helper)")
    return found


def check_nothing_else(allowed, found):
    extra = sorted(name for name in os.environ if name not in allowed)
    first = line(not found, "5. no ~/.aws, SSH keys or git credentials" + (f": {', '.join(found)}" if found else ""))
    second = line(not extra, "5. environment holds only the listed variables"
                  + (f", not {', '.join(extra)}" if extra else f" ({len(os.environ)} names)"))
    return first and second


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", required=True)
    parser.add_argument("--allowed-env", required=True)
    args = parser.parse_args()
    found = credential_files()
    scratch = Path("/tmp/checks")
    scratch.mkdir(exist_ok=True)
    results = [check_identity(), check_staging_logs(), check_production(scratch), check_platform(args.account),
               check_nothing_else(set(args.allowed_env.split(",")), found)]
    print(json.dumps({"passed": sum(results), "of": len(results)}))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
