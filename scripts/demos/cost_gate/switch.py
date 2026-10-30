"""The cost check's switch (video 18): the SSM String cost-check/enabled, off by default.

    python scripts/demos/cost_gate/switch.py on | off | status
    python3 scripts/demos/cost_gate/switch.py is-on orders-service    for the pipeline: prints on or off, never fails

Off means no Cost check stage, no line and no attachment, so the main video,
history.py and the other demos run exactly as before. On, a service whose
scripts name a cost_check in services.yaml gets the stage after Check
staging. The main reset turns it off; reset.py cost-gate turns it on.
Terraform makes the name (infra/parameters.yaml).
"""

import importlib.util
import sys
from pathlib import Path

# The AI notes' switch class, loaded by path: this file has the same name.
_spec = importlib.util.spec_from_file_location("ai_notes_switch", Path(__file__).resolve().parents[1] / "ai_notes" / "switch.py")
_switch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_switch)

SWITCH = _switch.Switch("cost-check/enabled", "Cost check",
                        lambda config: (config.get("scripts") or {}).get("cost_check"))

if __name__ == "__main__":
    sys.exit(SWITCH.main())
