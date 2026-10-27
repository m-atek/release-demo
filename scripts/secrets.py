"""See and set the platform's secrets in SSM Parameter Store.

    python scripts/secrets.py                 which parameters are set, never their values
    python scripts/secrets.py set slack       ask for every slack/ value you provide
    python scripts/secrets.py set jira/email  ask for one value

Values are read from the terminal without echo and go straight to SSM, as
SecureString unless infra/parameters.yaml says secure: false. They never reach
a file, a log or the screen. Uses AWS_PROFILE, by default release-demo.
"""

import argparse
import getpass
import re
import sys

import yaml

from common import ROOT, SSM_PREFIX as PREFIX, fail, put_parameter, ssm_client


def catalog():
    return yaml.safe_load((ROOT / "infra" / "parameters.yaml").read_text())


def versions(ssm):
    """Parameter name to version, from metadata only: nothing is decrypted."""
    found = {}
    pages = ssm.get_paginator("describe_parameters").paginate(
        ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [PREFIX]}])
    for page in pages:
        for parameter in page["Parameters"]:
            found[parameter["Name"].removeprefix(PREFIX)] = parameter["Version"]
    return found


def state(name, entry, found):
    """missing, unset or set. Terraform writes "unset" as version 1 of the others."""
    if name not in found:
        return "missing"
    if entry["source"] != "terraform" and found[name] == 1:
        return "unset"
    return "set"


def show_status(ssm):
    entries, found = catalog(), versions(ssm)
    width = max(map(len, entries))
    for name, entry in entries.items():
        print(f"  {name:<{width}}  {state(name, entry, found):<8} {entry['source']}")
    todo = [n for n, e in entries.items() if e["source"] == "you" and state(n, e, found) != "set"]
    print(f"\n{len(todo)} still to set by you." if todo else "\nEverything you provide is set.")
    if any(state(n, e, found) == "missing" for n, e in entries.items() if e["source"] != "terraform"):
        print("Some names are missing: apply infra/ first (scripts/tf.sh apply).")


def ask(name, entry):
    print(f"\n{name}: {entry['help']}")
    while True:
        value = getpass.getpass("  value (hidden, Enter to skip): ").strip()
        if not value:
            return None
        if entry.get("pattern") and not re.search(entry["pattern"], value):
            print("  That doesn't look right for this one. Try again, or Enter to skip.")
            continue
        return value


def set_values(ssm, selector, force):
    entries, found = catalog(), versions(ssm)
    chosen = {n: e for n, e in entries.items() if n == selector or n.startswith(selector.rstrip("/") + "/")}
    if not chosen:
        fail(f"No parameter matches {selector}. Run without arguments to see them all.")
    for name, entry in chosen.items():
        if entry["source"] != "you" and not force:
            print(f"\n{name}: written by {entry['source']}, skipped (--force to set it anyway)")
            continue
        if name not in found and entry["source"] != "terraform":
            fail(f"{PREFIX}{name} doesn't exist yet. Apply infra/ first (scripts/tf.sh apply).")
        if state(name, entry, found) == "set":
            print(f"\n{name}: already set. Enter skips it, a value replaces it.")
        value = ask(name, entry)
        if value is None:
            print("  skipped")
            continue
        put_parameter(ssm, name, value, entry["secure"])
        print("  saved")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    setter = sub.add_parser("set", help="ask for values")
    setter.add_argument("selector", help="a name, or a group such as slack")
    setter.add_argument("--force", action="store_true", help="also values a script or Terraform writes")
    args = parser.parse_args()

    ssm = ssm_client()
    if args.command == "set":
        set_values(ssm, args.selector, args.force)
    else:
        show_status(ssm)


if __name__ == "__main__":
    sys.exit(main())
