"""What deploy.py adds for video 33: when Helm gives up on pods the scheduler refused, Slack and the notes
say so in the scheduler's words. Nothing changes for a version that never gets ready (video 8), or a rehearsal."""

import subprocess
import urllib.error

import pytest

import deploy
import kube
import run_state
from common import ROOT, service

SAID = ("0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes are available: "
        "1 No preemption victims found for incoming pod.")
FIRST = "0/1 nodes are available: 1 Insufficient memory."
POD = "catalog-79c5d6f4c8-lm2xp"


def event(name=POD, message=SAID, created="2026-11-20T10:00:05Z"):
    return {"metadata": {"creationTimestamp": created}, "involvedObject": {"name": name}, "message": message}


class Api:
    def __init__(self, events=(), pods=()):
        self.events, self._pods, self.asked = list(events), list(pods), []

    def get(self, path):
        self.asked.append(path)
        return {"items": self.events}

    def pods(self, namespace, release):
        return self._pods


def test_the_schedulers_first_sentence_for_this_rollouts_pods():
    api = Api([event()])
    assert kube.scheduler_said(api, "staging", {POD}) == FIRST
    assert api.asked == ["/api/v1/namespaces/staging/events?fieldSelector=reason%3DFailedScheduling"]


def test_only_this_rollouts_pods_count():
    assert kube.scheduler_said(Api([event("catalog-old-take")]), "staging", {POD}) is None
    assert kube.scheduler_said(Api([]), "staging", {POD}) is None
    assert kube.scheduler_said(Api([event()]), "staging", set()) is None


def test_the_newest_word_wins():
    events = [event(message="0/1 nodes are available: 1 Too many pods.", created="2026-11-20T10:00:01Z"), event()]
    assert kube.scheduler_said(Api(events), "staging", {POD}) == FIRST


def test_a_missed_read_never_stops_a_deploy():
    class Down(Api):
        def get(self, path):
            raise urllib.error.URLError("refused")

    assert kube.scheduler_said(Down(), "staging", {POD}) is None


def revisions(after):
    before = [{"revision": 4, "status": "deployed", "app_version": "1.12.0"}]
    gave_up = [{"revision": 4, "status": "superseded", "app_version": "1.12.0"},
               {"revision": 5, "status": "failed", "app_version": "1.12.1"},
               {"revision": 6, "status": "deployed", "app_version": "1.12.0"}]
    return iter([before, gave_up if after == "rolled back" else
                 [{"revision": 4, "status": "superseded", "app_version": "1.12.0"},
                  {"revision": 5, "status": "deployed", "app_version": "1.12.1"}]])


@pytest.fixture
def upgrade(monkeypatch, tmp_path):
    """deploy.upgrade with Helm's answers and the API faked: returns what it ran, and the state it saved."""
    def run(events, state=None, after="rolled back", code=1, seconds=181):
        answers = revisions(after)
        ran = []
        clock = iter([0.0, 0.0])                  # the watch's start, then Helm's: after those, Helm's end
        monkeypatch.setattr(kube, "history", lambda kubeconfig, name, namespace: next(answers))
        monkeypatch.setattr(kube, "helm", lambda *args, **kwargs: subprocess.CompletedProcess(args, code, stdout="", stderr=""))
        monkeypatch.setattr(kube, "now", lambda: next(clock, seconds))
        api = Api(events, pods=[{"metadata": {"name": POD, "labels": {"app.kubernetes.io/version": "1.12.1"}}, "status": {"phase": "Pending"}}])
        monkeypatch.setattr(kube, "Api", lambda kubeconfig: api)
        monkeypatch.setattr(deploy, "rollback_image", lambda *args: "image@sha256:old")
        real = deploy.Watch

        class Watched(real):
            def start(self):
                self.new_pods.add(POD)   # what its first look at the pods finds, without the wait

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(deploy, "Watch", Watched)
        monkeypatch.setattr(deploy.subprocess, "run", lambda command, **kwargs: ran.append([str(part) for part in command]))
        state = state if state is not None else {"service": "catalog", "version": "1.12.1", "slack": {"channel": "C1", "ts": "1"}}
        release = {"version": "1.12.1", "image": {"digest": "sha256:new"}, "chart": {"repository": "helm-local/catalog", "digest": "sha256:chart"}}
        chart = tmp_path / "catalog.tgz"
        code_returned = deploy.upgrade(service("catalog"), "staging", release, chart, tmp_path, "kubeconfig", state)
        return code_returned, ran, state, run_state.load()

    return run


def test_helm_gave_up_on_a_refused_pod_slack_says_why_last(upgrade):
    code, ran, state, saved = upgrade([event()])
    assert code == 0
    record = saved["deploys"]["staging"]
    assert record["helm_rolled_back_to"] == "1.12.0"
    assert record["scheduler_said"] == FIRST
    assert record["rollback_notes"].endswith(f"The scheduler: {FIRST}")
    assert ran[-1][-3:] == [str(ROOT / "scripts" / "notify.py"), "line", f"Not ready after 3 minutes. The scheduler: {FIRST}"]
    assert len(ran) == 1  # no change yet in staging, so no note


def test_a_version_that_runs_and_never_gets_ready_is_untouched(upgrade):
    """Video 8's 1.9.0: no FailedScheduling, so no line and the notes as they were."""
    code, ran, state, saved = upgrade([])
    record = saved["deploys"]["staging"]
    assert code == 0 and record["helm_rolled_back_to"] == "1.12.0"
    assert "scheduler_said" not in record
    assert "The scheduler" not in record["rollback_notes"]
    assert ran == []


def test_a_rehearsal_or_a_reset_has_no_slack_message(upgrade):
    _, ran, _, saved = upgrade([event()], state={"service": "catalog", "version": "1.12.1"})
    assert saved["deploys"]["staging"]["scheduler_said"] == FIRST
    assert ran == []


def test_a_change_that_exists_gets_the_note_first_and_slack_last(upgrade):
    state = {"service": "catalog", "version": "1.12.1", "slack": {"channel": "C1", "ts": "1"}, "change": {"number": "CHG1"}}
    _, ran, _, _ = upgrade([event()], state=state)
    assert ran[0][-2] == "note" and f"The scheduler: {FIRST}" in ran[0][-1]
    assert ran[1][-2] == "line"


def test_a_good_deploy_asks_the_scheduler_nothing(upgrade):
    code, ran, _, saved = upgrade([event()], after="live", code=0, seconds=45)
    assert code == 0
    assert "scheduler_said" not in saved["deploys"]["staging"]
    assert ran == []
