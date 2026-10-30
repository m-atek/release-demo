"""reset.py ai-notes: when it releases 1.4.0 again, and when it only frees
what a take made."""

import json

import publish
import reset


def test_the_main_reset_steps_point_at_the_ai_notes_baseline():
    shared = reset.main_reset("1.4.0", ["ORD-13"])
    assert (shared.BASELINE, shared.DEMO_ISSUES) == ("1.4.0", ["ORD-13"])
    assert shared.after_baseline("1.4.1") and not shared.after_baseline("1.4.0")
    assert reset.main_reset().BASELINE == "1.3.0"


def jfrog(files):
    def request(method, path, token, data=None, headers=None):
        if path.startswith("api/storage/"):
            path = path.removeprefix("api/storage/")
            if path in files:
                return 200, b"{}"
            children = sorted({name[len(path) + 1:].split("/")[0] for name in files if name.startswith(path + "/")
                               and "/" in name[len(path) + 1:]})
            return (200, json.dumps({"children": [{"uri": f"/{c}", "folder": True} for c in children]}).encode()) \
                if any(name.startswith(path + "/") for name in files) else (404, b"{}")
        return (200, files[path]) if path in files else (404, b"")
    return request


def test_a_release_copy_counts_only_if_production_runs_its_package(monkeypatch):
    monkeypatch.setattr(reset, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(reset, "ssm_client", lambda: None)
    monkeypatch.setattr(publish, "stored_sha256", lambda token, path: "x" if path in files else None)
    files = {"orders-service-releases/1.4.0/build-info.json": json.dumps({"sha256": "aa"}).encode(),
             "orders-service-releases/1.4.0/71/build-info.json": json.dumps({"sha256": "bb"}).encode(),
             "orders-service-releases/1.4.0/71/cost.json": b"{}"}
    monkeypatch.setattr(publish, "request", jfrog(files))
    monkeypatch.setattr(reset, "live_hex", lambda: ("64", "1.4.0", "bb"))
    assert reset.release_copy() == "orders-service-releases/1.4.0/71"
    assert reset.release_copy(need_cost=True) == "orders-service-releases/1.4.0/71"
    monkeypatch.setattr(reset, "live_hex", lambda: ("60", "1.4.0", "aa"))
    assert reset.release_copy() == "orders-service-releases/1.4.0"
    assert reset.release_copy(need_cost=True) is None  # no cost check measured that one
    monkeypatch.setattr(reset, "live_hex", lambda: ("61", "1.4.1", "cc"))
    assert reset.release_copy() is None


def test_without_a_release_copy_it_resets_and_releases(monkeypatch, capsys):
    steps = []
    shared = reset.main_reset()
    monkeypatch.setattr(reset, "main_reset", lambda baseline=None, issues=None: shared)
    monkeypatch.setattr(reset, "released", lambda need_cost=False: None)
    monkeypatch.setattr(shared, "reset_main", lambda dry_run, slack_url=None: steps.append("main reset") or 0)
    monkeypatch.setattr(shared, "jira_versions", lambda r: steps.append("jira"))
    monkeypatch.setattr(reset, "set_switch", lambda r, name, value: steps.append(f"{name} {value}"))
    assert reset.prepare(True, "1.4.1", ["ORD-13"], {"ai-notes/enabled": "on"}) == 0
    out = capsys.readouterr().out
    assert steps == ["main reset", "jira", "ai-notes/enabled on"]
    assert "would stage orders-service 1.4.0 and release it under ORD-12 as a standard change" in out
    assert "would stage orders-service 1.4.1 on main" in out


def test_with_a_release_copy_it_only_frees_the_take(monkeypatch, capsys):
    freed = []
    monkeypatch.setattr(reset, "released", lambda need_cost=False: "orders-service-releases/1.4.0")
    monkeypatch.setattr(reset, "free_take", lambda r, shared, slack_url=None: freed.append(
        (shared.BASELINE, shared.DEMO_ISSUES)))
    monkeypatch.setattr(reset, "set_switch", lambda r, name, value: None)
    monkeypatch.setattr(reset, "release_baseline", lambda r: freed.append("released"))
    assert reset.prepare(True, "1.4.1", ["ORD-13"], {"ai-notes/enabled": "on"}) == 0
    assert freed == [("1.4.0", ["ORD-13"])]
    assert "no new release" in capsys.readouterr().out
