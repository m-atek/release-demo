"""The patch clock's switch (video 24): the SSM String patch-clock/enabled, off by default.

    python scripts/demos/patch_clock/switch.py on | off | status

Off, /sbom still answers and nothing else happens: no clock opens and a release
does nothing new, so every other video runs as it always has. On, the first
search that finds a holder opens the clock, and the closed change that removes
the last holder stops it. The main reset turns it off; reset.py patch-clock
turns it on. Terraform makes the name (infra/parameters.yaml).
"""

import importlib.util
import sys
from pathlib import Path

# The AI notes' switch class, loaded by path: this file has the same name.
_spec = importlib.util.spec_from_file_location("ai_notes_switch", Path(__file__).resolve().parents[1] / "ai_notes" / "switch.py")
_switch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_switch)

SWITCH = _switch.Switch("patch-clock/enabled", "Patch clock", lambda config: config.get("live"))

if __name__ == "__main__":
    sys.exit(SWITCH.main())
