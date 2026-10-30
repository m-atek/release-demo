"""reset.py <demo> and demo.sh find a demo's folder by its name, or by its name
with underscores: cost-gate is scripts/demos/cost_gate, agent-access stays."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reset  # noqa: E402


def test_reset_finds_the_folder_with_or_without_dashes(tmp_path, monkeypatch):
    calls = []
    for name in ("agent-access", "cost_gate"):
        (tmp_path / "scripts" / "demos" / name).mkdir(parents=True)
        (tmp_path / "scripts" / "demos" / name / "reset.py").write_text("")
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset.subprocess, "run", lambda command, cwd: calls.append(command[1]) or
                        subprocess.CompletedProcess(command, 0))
    assert reset.other_demo("agent-access", []) == 0 and reset.other_demo("cost-gate", []) == 0
    assert calls == [str(tmp_path / "scripts/demos/agent-access/reset.py"), str(tmp_path / "scripts/demos/cost_gate/reset.py")]


def test_demo_sh_maps_a_dash_only_without_a_folder_of_that_name():
    script = (Path(__file__).resolve().parents[1] / "demo.sh").read_text()
    assert '[[ -z $demo || -d $DEMOS/$demo ]] || demo=${demo//-/_}' in script
