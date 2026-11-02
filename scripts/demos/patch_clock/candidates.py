"""Which CVE video 24 shows, and the fallbacks of acceptance check 1, as data.

CHOSEN is the one the demo states hold. scripts/demos/patch_clock/use_fallback.py
switches it and their requirements.txt; first_day.py cve tries them in this order.
Nothing else in the demo names a CVE: the search takes any id, and Grype's database
gives its facts.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    cve: str
    ghsa: str
    package: str
    bad: str      # what 1.12.0 and 1.12.1 pin
    good: str     # what 1.12.2 pins

    @property
    def patch_summary(self):
        """ORD-[p]'s summary."""
        return f"Patch {self.package} to {self.good} ({self.cve})"


CANDIDATES = {
    "brotli": Candidate("CVE-2026-111497", "GHSA-9v8h-qqgm-4m35", "brotli", "1.2.0", "1.2.1"),
    "h11": Candidate("CVE-2025-43859", "GHSA-vqfr-h8mv-ghfj", "h11", "0.14.0", "0.16.0"),
    "urllib3": Candidate("CVE-2018-20060", "GHSA-www2-v7xj-xrc6", "urllib3", "1.22", "2.8.0"),
}
CHOSEN = "brotli"
THIS_FILE = Path(__file__)


def chosen():
    return CANDIDATES[CHOSEN]


def version_key(text):
    """(1, 2, 0) for "1.2.0", for comparing a pin with the fixed version."""
    return tuple(int(part) for part in text.split(".") if part.isdigit())
