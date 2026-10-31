"""tools-host/idle_stop.py powers the tools host off after 30 idle minutes. A
multibranch job's scan for branches isn't a build: network-stack and
catalog-production scan every minute, and while their scans waited out the
quiet period at the top of each minute, when the idle timer fires too, the
host counted as busy and stayed up all night (30 to 31 October)."""

import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MULTIBRANCH = "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"


def jenkins_busy(monkeypatch, tmp_path, running, queued):
    spec = importlib.util.spec_from_file_location("idle_stop", ROOT / "tools-host" / "idle_stop.py")
    idle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(idle)
    (tmp_path / "jenkins-admin-password").write_text("test")

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout):
        if "/computer/" in request.full_url:
            executors = [{"currentExecutable": build} for build in running]
            return Response(json.dumps({"computer": [{"executors": executors, "oneOffExecutors": []}]}).encode())
        return Response(json.dumps({"items": [{"task": task} for task in queued]}).encode())

    monkeypatch.setattr(idle.urllib.request, "urlopen", urlopen)
    return idle.jenkins_busy(tmp_path)


def test_the_minute_s_branch_scans_waiting_in_the_queue_dont_keep_the_host_up(monkeypatch, tmp_path):
    scans = [{"_class": MULTIBRANCH, "url": "https://jenkins-demo.es.nl/job/network-stack/"},
             {"_class": MULTIBRANCH, "url": "https://jenkins-demo.es.nl/job/catalog-production/"}]
    assert jenkins_busy(monkeypatch, tmp_path, [], scans) is False


def test_a_running_scan_doesnt_either_but_a_branch_s_build_does(monkeypatch, tmp_path):
    scan = {"_class": "jenkins.branch.MultiBranchProject$BranchIndexing",
            "url": "https://jenkins-demo.es.nl/job/network-stack/indexing/"}
    build = {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowRun",
             "url": "https://jenkins-demo.es.nl/job/network-stack/job/PR-3/1/"}
    queued_build = {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
                    "url": "https://jenkins-demo.es.nl/job/network-stack/job/PR-3/"}
    assert jenkins_busy(monkeypatch, tmp_path, [scan], []) is False
    assert jenkins_busy(monkeypatch, tmp_path, [scan, build], []) is True
    assert jenkins_busy(monkeypatch, tmp_path, [], [queued_build]) is True
