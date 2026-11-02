"""The scan's line in a release's Slack message: which critical finding stopped it, and what fixes it.

    python3 scripts/scan_line.py

The release's SBOM and scan stage runs it only after the stage failed, with
returnStatus, so a passing scan never runs it and it can't change how a release
ends. It reads .release/reports/grype.json and adds one line to the status
message: "Scan: critical CVE-2026-111497 in brotli 1.2.0." and under it "Fixed
in 1.2.1.". Grype names a finding by the advisory that matched, so the CVE comes
from relatedVulnerabilities when the match's id is a GHSA. Several findings end
"... and [n] more.", and one without a fix "No fixed version yet.". No report, or
no critical finding in it (the stage failed some other way): no line.
"""

import json
import sys

import notify
import run_state


def is_cve(name):
    return str(name or "").upper().startswith("CVE-")


def cve_of(match):
    """The CVE of a match, or the id it has when no CVE goes with it."""
    found = match["vulnerability"]["id"]
    if is_cve(found):
        return found
    return next((related["id"] for related in match.get("relatedVulnerabilities") or [] if is_cve(related.get("id"))), found)


def fix_of(match):
    """Grype's suggested version, or the first fixed version it lists."""
    for detail in match.get("matchDetails") or []:
        suggested = (detail.get("fix") or {}).get("suggestedVersion")
        if suggested:
            return suggested
    versions = (match["vulnerability"].get("fix") or {}).get("versions") or []
    return versions[0] if versions else None


def line(report):
    """The line for a Grype report, or None without a critical finding."""
    critical = [match for match in report.get("matches") or []
                if str(match["vulnerability"].get("severity", "")).lower() == "critical"]
    if not critical:
        return None
    findings = list(dict.fromkeys((cve_of(match), match["artifact"]["name"], match["artifact"]["version"])
                                  for match in critical))
    cve, name, version = findings[0]
    more = f" and {len(findings) - 1} more" if len(findings) > 1 else ""
    fix = fix_of(critical[0])
    return f"Scan: critical {cve} in {name} {version}{more}.\n" + (f"Fixed in {fix}." if fix else "No fixed version yet.")


def main():
    path = run_state.REPORTS / "grype.json"
    if not path.exists():
        print("No grype.json: the scan didn't report, so there's no line.")
        return 0
    text = line(json.loads(path.read_text()))
    if not text:
        print("No critical finding in grype.json, so there's no line.")
        return 0
    state = run_state.load()
    state.setdefault("lines", []).append(text)
    run_state.save(state)
    notify.post(state)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
