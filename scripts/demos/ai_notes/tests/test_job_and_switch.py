"""The ai-notes job's checks before it writes, the switch, and Sam's fix
(video 13's acceptance check 7) on the local runner."""

import importlib.util
import json
import threading

import pytest

import check
import job
import lambda_local
import notes
from common import ROOT, service
from switch import SWITCH, Switch


class FakeJob:
    job = "release"

    def __init__(self, files):
        self.files, self.asked = files, []

    def call(self, method, path, form=None):
        self.asked.append(path)
        name = path.split("/artifact/", 1)[1]
        return (200, {}, self.files[name]) if name in self.files else (404, {}, b"")


def test_the_job_takes_what_the_release_build_archived(tmp_path):
    fake = FakeJob({".release/evidence.json": b"{}", ".release/check-production.txt": b"FAIL / answered 502\n"})
    found = notes.fetch_archive(fake, "62", tmp_path)
    assert found == [".release/evidence.json", ".release/check-production.txt"]
    assert (tmp_path / "check-production.txt").read_text() == "FAIL / answered 502\n"
    assert fake.asked[0] == "/job/release/62/artifact/.release/evidence.json"


def test_the_job_writes_only_to_the_change_of_that_release_build(tmp_path, monkeypatch):
    evidence = {"run": {"service": "orders-service", "change_number": "CHG0030071", "change_sys_id": "c" * 32}}
    monkeypatch.setattr(job, "FOLDER", tmp_path)
    monkeypatch.setattr(notes, "fetch_archive", lambda release, build, folder: (folder / "evidence.json").write_text(
        json.dumps(evidence)) and [".release/evidence.json"])
    monkeypatch.setattr("release_job.ReleaseJob.as_release_bot", classmethod(lambda cls, job="release": None))
    for name, value in {"KIND": "summary", "RELEASE_BUILD": "62", "SERVICE": "orders-service",
                        "CHANGE_NUMBER": "CHG0030099", "CHANGE_SYS_ID": "c" * 32}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(job.summarize, "main", lambda: pytest.fail("wrote to another change"))
    with pytest.raises(SystemExit, match="not the orders-service CHG0030099"):
        job.main()
    monkeypatch.setenv("CHANGE_NUMBER", "CHG0030071")
    monkeypatch.setattr(job.summarize, "main", lambda: 0)
    assert job.main() == 0


def test_without_evidence_the_job_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "FOLDER", tmp_path)
    monkeypatch.setattr(notes, "fetch_archive", lambda release, build, folder: [])
    monkeypatch.setattr("release_job.ReleaseJob.as_release_bot", classmethod(lambda cls, job="release": None))
    monkeypatch.setenv("KIND", "cause")
    monkeypatch.setenv("RELEASE_BUILD", "62")
    assert job.main() == 0


class FakeSsm:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, value=None):
        self.value, self.put = value, []

    def get_parameter(self, Name):
        if self.value is None:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, **kwargs):
        self.put.append(kwargs)
        self.value = kwargs["Value"]


def test_the_switch_is_off_unless_it_says_on(monkeypatch):
    assert not SWITCH.is_on(FakeSsm("unset")) and not SWITCH.is_on(FakeSsm()) and SWITCH.is_on(FakeSsm("on"))
    assert SWITCH.on_for("orders-service", FakeSsm("on"))
    assert not SWITCH.on_for("pricing-api", FakeSsm("on"))  # no ai_notes in services.yaml
    assert not SWITCH.on_for("no-such-service", FakeSsm("on"))
    ssm = FakeSsm("unset")
    SWITCH.set("on", ssm)
    assert ssm.put == [{"Name": "/release-demo/ai-notes/enabled", "Value": "on", "Type": "String", "Overwrite": True}]
    with pytest.raises(SystemExit):
        SWITCH.set("on", FakeSsm())  # Terraform makes the name first


def test_is_on_never_fails_the_pipeline(monkeypatch, capsys):
    broken = Switch("ai-notes/enabled", "AI notes", lambda config: True)
    monkeypatch.setattr("switch.ssm_client", lambda: (_ for _ in ()).throw(SystemExit(1)))
    assert broken.main(["is-on", "orders-service"]) == 0
    assert capsys.readouterr().out == "off\n"


def load_state(version):
    spec = importlib.util.spec_from_file_location(f"orders_{version.replace('.', '_')}",
                                                  ROOT / "demo-states" / "orders-service" / version / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("extra, passes", [({"DELIVERY_ESTIMATE_DAYS": "2"}, True), ({}, False)])
def test_sams_fix_production_s_settings_plus_the_new_name_pass_production_s_check(monkeypatch, capsys, extra, passes):
    production = service("orders-service")["environments"]["production"]
    for name in ("DELIVERY_ESTIMATE_DAYS", "DELIVERY_DAYS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"ENVIRONMENT": "production", "DEPLOYED_AT": "30 Oct 2026, 10:00 UTC",
                        **production["settings"], **extra}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(production["check"], "timeout_seconds", 5)
    monkeypatch.setattr(check, "service", lambda name: {"environments": {"production": production}})
    server = lambda_local.make_server(load_state("1.4.1").handler, "production", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        monkeypatch.setattr("sys.argv", ["check.py", "orders-service", "production", "--version", "1.4.1", "--url", url])
        if passes:
            check.main()
            assert "Production check passed" in capsys.readouterr().out
        else:
            with pytest.raises(SystemExit):
                check.main()
            assert "FAIL / answered 502" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
