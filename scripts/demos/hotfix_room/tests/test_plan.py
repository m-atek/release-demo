"""The Plan stage: the numbers on the change, from the pods, their cgroups and the chart; a failed read says so and never stops a release."""

import contextlib
import datetime
import json

import pytest

import kube
import plan
import room
import run_state
from common import ROOT, service
from fake_node import MIB

NOW = datetime.datetime(2026, 11, 20, 16, 52, tzinfo=datetime.timezone.utc)
UID = "6f1c2a3b-1111-2222-3333-444455556666"


def fake_pod(index=0, request="256Mi", uid=UID, started="2026-11-20T16:38:00Z", deleting=False):
    metadata = {"name": f"forecast-7d9f8b6c5-{index}", "uid": uid, "labels": {"app.kubernetes.io/version": "2.4.0"}}
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-20T16:50:00Z"
    return {"metadata": metadata, "spec": {"containers": [{"resources": {"requests": {"memory": request}}}]},
            "status": {"phase": "Running", "startTime": started}}


def cgroup(root, relative, current=17 * MIB, peak=21 * MIB):
    folder = root / relative
    folder.mkdir(parents=True)
    (folder / "memory.current").write_text(f"{current}\n")
    if peak is not None:
        (folder / "memory.peak").write_text(f"{peak}\n")
    return folder


@pytest.mark.parametrize("relative", [
    f"kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{UID.replace('-', '_')}.slice",
    f"kubepods.slice/kubepods-pod{UID.replace('-', '_')}.slice",
    f"kubepods/burstable/pod{UID}",
    f"kubepods/pod{UID}"])
def test_a_pods_cgroup_under_either_driver_and_either_class(tmp_path, relative):
    folder = cgroup(tmp_path, relative)
    assert plan.pod_cgroup(UID, tmp_path) == folder
    assert plan.memory_of(fake_pod(), tmp_path) == (17 * MIB, 21 * MIB)


def test_no_memory_peak_on_an_older_kernel_reads_the_current_figure(tmp_path):
    cgroup(tmp_path, f"kubepods/burstable/pod{UID}", peak=None)
    assert plan.memory_of(fake_pod(), tmp_path) == (17 * MIB, 17 * MIB)


def test_no_cgroup_is_an_error_the_stage_turns_into_words(tmp_path):
    with pytest.raises(FileNotFoundError, match="no cgroup"):
        plan.memory_of(fake_pod(), tmp_path)


def test_the_lines_alex_reads():
    pods = [fake_pod(i, uid=f"uid-{i}") for i in range(3)]
    details, row = plan.lines_for("forecast", pods, 144 * MIB, 3, NOW, memory=lambda pod: (17 * MIB, 21 * MIB))
    assert details == ["Reservation in forecast, read from the node at 17:52:",
                       "Reserved 768 MiB in 3 pods. Used 51 MiB now, 63 MiB at its peak since these pods started, 14 minutes ago.",
                       "This release reserves 144 MiB in 3 pods and gives back 624 MiB."]
    assert row == "Reserved 768 MiB, used 51 MiB. Asks for 144 MiB."
    assert len(row) <= 53


def test_a_used_that_cant_be_read_says_so_and_keeps_what_it_knows():
    def refused(pod):
        raise FileNotFoundError("no cgroup for forecast-1")

    details, row = plan.lines_for("forecast", [fake_pod()], 48 * MIB, 3, NOW, memory=refused)
    assert "Used: not read (no cgroup for forecast-1)." in details[1]
    assert "gives back 208 MiB" in details[2]
    assert row == "Reserved 256 MiB. Asks for 48 MiB."


def test_what_the_release_asks_for_comes_from_the_chart_it_deploys(tmp_path):
    state = ROOT / "demo-states/forecast/2.4.1"
    assert plan.asked_by_chart({"dir": state}, 3) == 144 * MIB
    assert plan.asked_by_chart({"dir": ROOT / "demo-states/forecast/2.4.0"}, 3) == 768 * MIB


class Api:
    def __init__(self, pods=(), events=()):
        self._pods, self.events = list(pods), list(events)

    def pods(self, namespace, release):
        assert (namespace, release) == ("forecast", "forecast")
        return self._pods

    def get(self, path):
        assert path == "/api/v1/namespaces/staging/events?fieldSelector=reason%3DFailedScheduling"
        return {"items": self.events}


def event(created="2026-11-20T16:45:00Z", message="0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes are available: 1 No preemption victims found for incoming pod."):
    return {"metadata": {"creationTimestamp": created}, "message": message}


def test_the_schedulers_last_refusal_in_its_own_words():
    line = plan.scheduler_line(Api(events=[event("2026-11-20T16:10:00Z", "older."), event()]), "staging", NOW)
    assert line == "The scheduler, in staging at 17:45: 0/1 nodes are available: 1 Insufficient memory."


def test_a_refusal_over_an_hour_old_isnt_why():
    assert plan.scheduler_line(Api(events=[event("2026-11-20T15:40:00Z")]), "staging", NOW) is None
    assert plan.scheduler_line(Api(), "staging", NOW) is None


def run_main(monkeypatch, api):
    """As the stage runs it, with 2.4.1 on main, as a take has it."""
    run_state.save({"service": "forecast", "version": "2.4.1"})
    monkeypatch.setattr(plan.kube, "credentials", lambda quiet=False: contextlib.nullcontext("kubeconfig"))
    monkeypatch.setattr(plan.kube, "Api", lambda kubeconfig: api)
    monkeypatch.setattr(plan, "memory_of", lambda pod, root=None: (17 * MIB, 21 * MIB))
    monkeypatch.setattr(plan, "service", lambda name: {**service(name), "dir": ROOT / "demo-states/forecast/2.4.1"})
    monkeypatch.setattr(plan.sys, "argv", ["plan.py", "forecast"])
    assert plan.main() == 0
    return run_state.load()


def test_the_stage_writes_the_change_and_slack(monkeypatch):
    pods = [fake_pod(i, uid=f"uid-{i}", started="2026-11-05T13:38:00Z") for i in range(3)] + [fake_pod(9, deleting=True)]
    state = run_main(monkeypatch, Api(pods, [event(created=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))]))
    assert state["change_details"][0].startswith("Reservation in forecast, read from the node at ")
    assert "Reserved 768 MiB in 3 pods." in state["change_details"][1]  # not the one going away
    assert state["change_details"][2] == "This release reserves 144 MiB in 3 pods and gives back 624 MiB."
    assert "0/1 nodes are available: 1 Insufficient memory." in state["change_details"][3]
    assert state["rows"] == ["Reserved 768 MiB, used 51 MiB. Asks for 144 MiB."]


def test_no_pods_says_so_and_the_release_goes_on(monkeypatch):
    state = run_main(monkeypatch, Api())
    assert state["change_details"] == ["Reservation in forecast: not read (RuntimeError: no forecast pods in forecast)."]
    assert state["rows"] == ["Reservation not read."]


def test_the_cluster_not_answering_says_so_and_the_release_goes_on(monkeypatch):
    class Down(Api):
        def pods(self, namespace, release):
            raise OSError("connection refused")

    state = run_main(monkeypatch, Down())
    assert "not read (OSError: connection refused)" in state["change_details"][0]


def test_the_lines_reach_the_description_and_the_row_reaches_slack():
    """change.py adds change_details to the description; notify.py shows rows under the stages."""
    assert "change_details" in (ROOT / "scripts/change.py").read_text()
    assert 'state.get("rows")' in (ROOT / "scripts/notify.py").read_text()


def test_forecasts_release_has_a_plan_stage_and_no_other_new_stage():
    stages = run_state.stages_for(service("forecast"))
    assert stages == ["Build", "Test", "Quality gate", "SBOM and scan", "Publish", "Staging", "Check staging", "Plan", "Change",
                      "Approval", "Production", "Check production", "Record"]


def test_catalog_has_no_plan_stage():
    assert "Plan" not in run_state.stages_for(service("catalog"))


def test_the_pipeline_already_runs_a_plan_and_skips_a_missing_staging():
    pipeline = (ROOT / "jenkins/shared-library/vars/releasePipeline.groovy").read_text()
    assert "if (cfg.scripts.plan)" in pipeline
    assert "cfg.environments?.containsKey('staging')" in pipeline
    assert json.dumps("plan") in json.dumps(service("forecast")["scripts"])


def test_it_never_needs_a_right_the_pipeline_doesnt_have():
    source = (plan.Path(plan.__file__)).read_text()
    assert "kubectl" not in source and "nodes/proxy" not in source
    assert kube.SERVICE_ACCOUNT.endswith("jenkins:release-pipeline")
    assert room.STAGING == "staging"
