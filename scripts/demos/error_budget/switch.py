"""The error budget's switch (video 27): the SSM String error-budget/enabled, off by default.

    python scripts/demos/error_budget/switch.py on | off | status
    python3 scripts/demos/error_budget/switch.py is-on orders-service    for the pipeline: prints on or off, never fails

Off means no Error budget stage, no line and no event, so the main video,
history.py and the other demos run exactly as before. On, a service whose
scripts name an error_budget in services.yaml gets the stage first, before
Build. The main reset and scripts/demo.sh down error-budget turn it off;
reset.py error-budget turns it on. Terraform makes the name
(infra/parameters.yaml). The traffic has its own switch, error-budget/traffic-until.
"""

import importlib.util
import sys
from pathlib import Path

# The AI notes' switch class, loaded by path: this file has the same name.
_spec = importlib.util.spec_from_file_location("ai_notes_switch", Path(__file__).resolve().parents[1] / "ai_notes" / "switch.py")
_switch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_switch)

SWITCH = _switch.Switch("error-budget/enabled", "Error budget",
                        lambda config: (config.get("scripts") or {}).get("error_budget"))

if __name__ == "__main__":
    sys.exit(SWITCH.main())
