"""reset.py cost-gate: where video 18 starts. Production on 1.4.0 from a real
release whose cost check measured it, 1.7.0 staged on main, the switch on.

    python scripts/reset.py cost-gate              from the Mac it runs itself on the tools host
    python scripts/reset.py cost-gate --dry-run    every action it would take, and nothing else
    or the Jenkins job reset with DEMO=cost-gate

The real release step is reset.py ai-notes': when production already runs
1.4.0 from a release copy that holds its cost.json, only what a take made is
freed (open changes cancelled, the takes' tags, release copies and Jira
versions after 1.4.0 archived and un-released, ORD-[m] reopened, both
aliases on 1.4.0). Otherwise the main reset, the cost check switched on, a
real release of 1.4.0 under ORD-12 as a standard change, whose cost check
gives 1.4.0's measurement, its cost.json in JFrog and its bar in Grafana,
then ORD-[m] reopened. Either way 1.7.0 goes on main and the cost check's
switch stays on, the AI notes' off. Stored delivery messages stay: they are
1.7.1's store. About 8 minutes the first time, 2 the second.
"""

import importlib.util
import sys
from pathlib import Path

import setup

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ai_notes_reset", HERE.parent / "ai_notes" / "reset.py")
ai_notes = importlib.util.module_from_spec(_spec)
sys.path.insert(1, str(HERE.parent / "ai_notes"))
_spec.loader.exec_module(ai_notes)

STAGED = "1.7.0"


def issue_key():
    from jira import Jira

    key = setup.find_issue(Jira())
    if not key:
        raise SystemExit(f"Jira has no issue \"{setup.SUMMARY}\" yet: run scripts/demos/cost_gate/setup.py first.")
    return key


if __name__ == "__main__":
    if "--here" not in sys.argv and not ai_notes.host.on_host():
        sys.exit(ai_notes.main(script="demos/cost_gate/reset.py", unit="reset-cost-gate"))
    sys.exit(ai_notes.main(script="demos/cost_gate/reset.py", unit="reset-cost-gate", staged=STAGED,
                           issues=[issue_key()], need_cost=True, title="cost-gate",
                           switches={"cost-check/enabled": "on", "ai-notes/enabled": "off"}))
