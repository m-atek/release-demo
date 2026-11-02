"""Switch video 24 to another CVE, when acceptance check 1 says the chosen one doesn't stand.

    python scripts/demos/patch_clock/use_fallback.py h11       brotli | h11 | urllib3

Rewrites requirements.txt in the three demo states, so 1.12.0 and 1.12.1 hold the package's bad
version and 1.12.2 its good one, and CHOSEN in candidates.py, which setup.py and status read. The diff
from 1.12.1 to 1.12.2 stays one line and VERSION. With a fallback, brotli stays at its fixed version:
/api/orders still compresses, and the fallback's package is only declared, as 1.4.0-broken-scan
declares urllib3. It changes files only; it prints what comes next.
"""

import re
import sys
from pathlib import Path

import candidates

ROOT = Path(__file__).resolve().parents[3]
STATES = ROOT / "demo-states" / "orders-service"
COMPRESS = "# Compresses /api/orders for the store apps.\n"
DECLARED = {"h11": "# HTTP/1.1 for the carrier tracking client.\n", "urllib3": "# HTTP client for the carrier tracking API.\n"}


def requirements(name, version):
    if name == "brotli":
        return f"{COMPRESS}brotli=={version}\n"
    return f"{COMPRESS}brotli=={candidates.CANDIDATES['brotli'].good}\n{DECLARED[name]}{candidates.CANDIDATES[name].package}=={version}\n"


def switch(name, states=STATES, source=candidates.THIS_FILE):
    chosen = candidates.CANDIDATES[name]
    for state, version in (("1.12.0", chosen.bad), ("1.12.1", chosen.bad), ("1.12.2", chosen.good)):
        (states / state / "requirements.txt").write_text(requirements(name, version))
    text = Path(source).read_text()
    Path(source).write_text(re.sub(r'^CHOSEN = "[a-z0-9]+"$', f'CHOSEN = "{name}"', text, flags=re.M))
    return chosen


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in candidates.CANDIDATES:
        print(f"Usage: use_fallback.py {' | '.join(candidates.CANDIDATES)}")
        return 2
    chosen = switch(sys.argv[1])
    print(f"The demo states now hold {chosen.package} {chosen.bad}, then {chosen.good}: {chosen.cve}, {chosen.ghsa}. Next:")
    print("1. Commit and push this.")
    print("2. As admin in JFrog, delete orders-service-candidates/1.12.0/seed/: the package changes.")
    print("3. .venv/bin/python scripts/demos/patch_clock/setup.py: ORD-[p] gets its new summary, and the seed runs again.")
    print("4. .venv/bin/python scripts/reset.py patch-clock, then the script's fallback words (Rules, Fallback lines).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
