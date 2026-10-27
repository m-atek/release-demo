"""The checks every pull request on the network stack runs, each published as
its own GitHub check run, which the ruleset on main requires.

    python scripts/demos/terraform/checks.py static     terraform fmt, terraform validate, tflint and Checkov
    python scripts/demos/terraform/checks.py conftest   the house rules, against the plan in .release/plan.json

Runs in the Terraform toolchain image. Each check writes .release/checks/<n>.json
with its conclusion, the title the pull request shows next to its name, a
summary, the details, and the short failures Slack lists. The Jenkinsfile
publishes them with publishChecks. The repository is public, so the output
names files and resources, never values from the AWS account.
"""

import argparse
import json
import subprocess
import sys

import stack

CHECKS = ["terraform fmt", "terraform validate", "tflint", "Checkov", "Conftest"]
# The house rules in infra/demos/2-terraform/checks/policy/house.rego, by the
# name each message starts with.
HOUSE_RULES = ["Required tags", "S3 bucket must not be public", "Allowed regions", "Allowed instance types"]
OUT = stack.WORK / "checks"


def slug(name):
    return name.lower().replace(" ", "-")


def result_path(name):
    return OUT / f"{slug(name)}.json"


def record(name, ok, title, summary, text="", failures=()):
    result = {"name": name, "conclusion": "success" if ok else "failure", "title": title,
              "summary": stack.scrub(summary), "text": stack.scrub(text)[:60000], "failures": list(failures)}
    stack.write_json(result_path(name), result)
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {title}")
    return result


def run(command, cwd=stack.ROOT, env=None):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env)


def fence(text, language=""):
    return f"```{language}\n{text.strip()}\n```" if text.strip() else ""


def fmt():
    result = run(["terraform", f"-chdir={stack.STACK}", "fmt", "-check", "-recursive", "-diff", "-no-color"])
    files = [line for line in result.stdout.splitlines() if line.endswith(".tf") and not line.startswith(("---", "+++"))]
    if result.returncode == 0:
        return record("terraform fmt", True, "Formatted", "Every file is formatted as terraform fmt writes it.")
    return record("terraform fmt", False, f"{len(files) or 'Some'} file{'s' if len(files) != 1 else ''} not formatted",
                  "Run `terraform fmt` on:\n" + "\n".join(f"- `{f}`" for f in files),
                  fence(result.stdout + result.stderr, "diff"), failures=files or ["not formatted"])


def validate():
    data = stack.WORK / "terraform" / "validate"
    init = stack.terraform("init", "-backend=false", "-input=false", "-no-color", data_dir=data,
                           capture=True, check=False)
    if init.returncode != 0:
        return record("terraform validate", False, "terraform init failed", "The configuration doesn't initialise.",
                      fence(init.stdout + init.stderr), failures=["terraform init failed"])
    result = stack.terraform("validate", "-json", "-no-color", data_dir=data, capture=True, check=False)
    report = json.loads(result.stdout or "{}")
    problems = []
    for item in report.get("diagnostics", []):
        where = item.get("range", {})
        place = f"{where.get('filename', '')}:{where.get('start', {}).get('line', '')}" if where else ""
        problems.append(f"{item['severity']}: {item['summary']} {place}".strip())
    if report.get("valid"):
        warnings = len(problems)
        return record("terraform validate", True, "Valid" + (f", {warnings} warnings" if warnings else ""),
                      "The configuration is valid.", "\n".join(f"- {p}" for p in problems))
    errors = report.get("error_count", len(problems))
    return record("terraform validate", False, f"{errors} error{'s' if errors != 1 else ''}",
                  "terraform validate found errors.", "\n".join(f"- {p}" for p in problems), failures=problems[:5])


def tflint():
    result = run(["tflint", f"--chdir={stack.STACK}", f"--config={stack.CHECKS / 'tflint.hcl'}", "--format=json"])
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return record("tflint", False, "tflint didn't run", "tflint failed before checking.",
                      fence(result.stdout + result.stderr), failures=["tflint didn't run"])
    issues = [f"{i['rule']['name']}: {i['message']} ({i['range']['filename']}:{i['range']['start']['line']})"
              for i in report.get("issues", [])]
    errors = [e.get("message", str(e)) for e in report.get("errors", [])]
    if not issues and not errors:
        return record("tflint", True, "No issues", "tflint found nothing, with the Terraform and AWS rules.")
    found = issues + errors
    return record("tflint", False, f"{len(found)} issue{'s' if len(found) != 1 else ''}",
                  "tflint found:", "\n".join(f"- {line}" for line in found), failures=found[:5])


def checkov():
    result = run(["checkov", "-d", str(stack.STACK), "--config-file", str(stack.CHECKS / "checkov.yaml"),
                  "-o", "json", "--quiet", "--compact"])
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return record("Checkov", False, "Checkov didn't run", "Checkov failed before checking.",
                      fence(result.stdout[-3000:] + result.stderr[-3000:]), failures=["Checkov didn't run"])
    reports = report if isinstance(report, list) else [report]
    failed, passed = [], 0
    for one in reports:
        results = one.get("results", {})
        failed += results.get("failed_checks", [])
        passed += one.get("summary", {}).get("passed", 0)
    if not failed:
        return record("Checkov", True, f"{passed} passed", f"Checkov passed {passed} checks. Skipped checks and "
                      "their reasons: infra/demos/2-terraform/checks/checkov.yaml.")
    failed.sort(key=lambda c: (c["check_id"].startswith("CKV2"), c["check_id"]))
    names = []
    for check in failed:
        name = f"{check['check_id']} {check['check_name']}"
        if name not in names:
            names.append(name)
    rows = [f"| {c['check_id']} | {c['check_name']} | `{c['resource']}` | `{c['file_path'].lstrip('/')}:"
            f"{c['file_line_range'][0]}` |" for c in failed]
    table = "| Check | What | Resource | Where |\n| --- | --- | --- | --- |\n" + "\n".join(rows)
    title = f"{len(failed)} failed: " + ", ".join(c["check_id"] for c in failed)
    return record("Checkov", False, title[:120], f"{len(failed)} failed, {passed} passed.", table, failures=names)


def conftest():
    plan = stack.WORK / "plan.json"
    if not plan.exists():
        return record("Conftest", False, "No plan to check", "The house rules check the plan, and the plan failed. "
                      "See the Plan check.", failures=["no plan to check"])
    result = run(["conftest", "test", "--no-color", "--output", "json", "--policy", str(stack.CHECKS / "policy"),
                  str(plan)])
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return record("Conftest", False, "Conftest didn't run", "Conftest failed before checking.",
                      fence(result.stdout + result.stderr), failures=["Conftest didn't run"])
    messages = [failure["msg"] for one in report for failure in one.get("failures") or []]
    broken = [rule for rule in HOUSE_RULES if any(m.startswith(rule + ":") for m in messages)]
    broken += sorted({m.split(":")[0] for m in messages} - set(broken))
    lines = [f"{'❌' if rule in broken else '✅'} {rule}" for rule in HOUSE_RULES]
    if not messages:
        return record("Conftest", True, f"{len(HOUSE_RULES)} house rules pass", "\n".join(lines))
    return record("Conftest", False, "Failed: " + ", ".join(broken), "\n".join(lines),
                  "\n".join(f"- {m}" for m in messages), failures=broken)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("which", choices=["static", "conftest"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.which == "static":
        for check in (fmt, validate, tflint, checkov):
            check()
    else:
        conftest()


if __name__ == "__main__":
    sys.exit(main())
