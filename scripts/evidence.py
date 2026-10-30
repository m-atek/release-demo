"""Readers of a release's evidence, deterministic and with no AI: the JUnit
report, Grype's scan, the CycloneDX SBOM, Lambda's CodeSha256, and the
SHA-256 picker. The auditor sample (video 11) uses them; the AI notes
(videos 12 and 13) add the run's fact file to this script.
"""

import base64
import hashlib
import json
import xml.etree.ElementTree as ElementTree
from pathlib import Path

SEVERITIES = ["critical", "high", "medium", "low", "negligible", "unknown"]


def junit_counts(path):
    """total, passed, failed, errors and skipped from a JUnit report."""
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    count = {name: sum(int(suite.get(name, 0)) for suite in suites) for name in ("tests", "failures", "errors", "skipped")}
    return {"total": count["tests"], "passed": count["tests"] - count["failures"] - count["errors"] - count["skipped"],
            "failed": count["failures"], "errors": count["errors"], "skipped": count["skipped"]}


def grype_counts(path):
    """Grype's findings by severity, and their total."""
    matches = json.loads(Path(path).read_text()).get("matches") or []
    counts = dict.fromkeys(SEVERITIES, 0)
    for match in matches:
        severity = str(match.get("vulnerability", {}).get("severity") or "unknown").lower()
        counts[severity if severity in counts else "unknown"] += 1
    return {**counts, "total": len(matches)}


def sbom_components(path):
    """How many components a CycloneDX SBOM lists."""
    return len(json.loads(Path(path).read_text()).get("components") or [])


def base64_to_hex(value):
    """Lambda's CodeSha256 (base64) as the hex digest build-info.json records."""
    return base64.b64decode(value).hex()


def ranked(seed, keys):
    """keys in the order of SHA-256 of "<seed>:<key>": the same seed, the same order."""
    return sorted(keys, key=lambda key: hashlib.sha256(f"{seed}:{key}".encode()).hexdigest())
