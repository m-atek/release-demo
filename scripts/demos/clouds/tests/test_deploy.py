"""Production cloud by cloud, the stop at a failed check, and moving back,
with fake clouds: what the video's 1.2.1 run does, without the clouds."""

import check
import deploy
import pytest
import run_state
from common import service
from targets import Revision, Target, suffix, version_in


class FakeCloud(Target):
    """A target that keeps its revisions in memory. healthy=False fails its check."""

    def __init__(self, key, name, versions, healthy=True):
        self.key, self.name, self.environment = key, name, "production"
        self.history = [Revision(f"{key}-{i}", v) for i, v in enumerate(versions, 1)][::-1]
        self.live_id = self.history[0].id
        self.healthy = healthy
        self.moves = []

    def url(self):
        return f"https://{self.key}.example"

    def live(self):
        return next(r for r in self.history if r.id == self.live_id)

    def deploy(self, digest, version, label):
        revision = Revision(f"{self.key}-{len(self.history) + 1}", version)
        self.history.insert(0, revision)
        self.live_id = revision.id
        return revision

    def move(self, revision_id):
        self.moves.append(revision_id)
        self.live_id = revision_id

    def revisions(self):
        return self.history


@pytest.fixture
def config():
    return service("quote-service")


@pytest.fixture
def clouds():
    return {"gcp": FakeCloud("gcp", "Google Cloud", ["1.1.0", "1.2.0"]),
            "azure": FakeCloud("azure", "Azure", ["1.1.0", "1.2.0"], healthy=False),
            "aws": FakeCloud("aws", "AWS", ["1.1.0", "1.2.0"])}


@pytest.fixture(autouse=True)
def fakes(monkeypatch, clouds):
    notes = []
    monkeypatch.setattr(deploy, "check_cloud", lambda url, key, *args, **kwargs: clouds[key].healthy)
    monkeypatch.setattr(deploy, "note_change", notes.append)
    monkeypatch.setattr(deploy, "sign_in_all", lambda config, keys, env: {k: clouds[k] for k in keys})
    return notes


def test_production_goes_in_order_and_stops_at_the_failed_check(config, clouds):
    targets = [clouds[k] for k in ("gcp", "azure", "aws")]
    state, stopped_at = deploy.deploy_environment(config, "production", targets, "sha256:new", "1.2.1", "b17")
    assert stopped_at == "Azure"
    assert clouds["gcp"].live().version == "1.2.1"
    assert state["azure"]["check"] == "failed"
    assert state["aws"]["status"] == "not released"
    assert clouds["aws"].live().version == "1.2.0"  # never touched
    assert deploy.summary(state) == "Google Cloud 1.2.1, Azure 1.2.1 (failed its check), AWS 1.2.0 (not released)"


def test_back_moves_only_the_failed_cloud_and_notes_each_version(config, clouds, fakes):
    targets = [clouds[k] for k in ("gcp", "azure", "aws")]
    infos, _ = deploy.deploy_environment(config, "production", targets, "sha256:new", "1.2.1", "b17")
    state = {"deploys": {"production": {"clouds": infos}}}
    deploy.back(config, "production", ["gcp", "azure", "aws"], state)
    assert clouds["azure"].live().version == "1.2.0"
    assert clouds["gcp"].live().version == "1.2.1" and not clouds["gcp"].moves
    assert not clouds["aws"].moves
    production = run_state.load()["deploys"]["production"]
    assert production["rolled_back_to"] == "1.2.0"
    assert production["summary"] == "Google Cloud 1.2.1, Azure 1.2.0 (rolled back), AWS 1.2.0 (not released)"
    assert "Azure" in fakes[-1] and "AWS 1.2.0" in fakes[-1]


def test_after_the_rollback_the_check_expects_each_clouds_own_version(config, clouds):
    targets = [clouds[k] for k in ("gcp", "azure", "aws")]
    infos, _ = deploy.deploy_environment(config, "production", targets, "sha256:new", "1.2.1", "b17")
    state = {"deploys": {"production": {"clouds": infos}}}
    deploy.back(config, "production", ["gcp", "azure", "aws"], state)
    plan = check.plan(config, "production", run_state.load(), "1.2.0", "")
    assert [(cloud, expected) for cloud, _, expected, _ in plan] == [("gcp", "1.2.1"), ("azure", "1.2.0"), ("aws", "1.2.0")]


def test_staging_goes_to_every_cloud_without_stopping(config, clouds):
    targets = [clouds[k] for k in ("gcp", "azure", "aws")]
    state, stopped_at = deploy.deploy_environment(config, "staging", targets, "sha256:new", "1.2.1", "b17")
    assert stopped_at is None
    assert all(c["status"] == "released" for c in state.values())


def test_previous_moves_only_the_clouds_on_the_newest_version(config, clouds):
    clouds["gcp"].deploy("sha256:new", "1.2.1", "b17")
    deploy.previous(config, "production", ["gcp", "azure", "aws"], {})
    assert clouds["gcp"].live().version == "1.2.0"
    assert not clouds["azure"].moves and not clouds["aws"].moves
    production = run_state.load()["deploys"]["production"]
    assert (production["previous_version"], production["rolled_back_to"]) == ("1.2.1", "1.2.0")


def test_the_clouds_variable_narrows_a_run(config, monkeypatch):
    monkeypatch.setenv("CLOUDS", "aws")
    assert deploy.clouds_in_order(config) == ["aws"]
    assert [c["key"] for c in config["clouds"]] == ["gcp", "azure", "aws"]


def test_revision_names_carry_the_version():
    assert suffix("1.2.0", "b17") == "v1-2-0-b17"
    assert version_in("quote-service-production--v1-2-0-b17") == "1.2.0"
    assert version_in("quote-service-staging-v1-1-0-initial") == "1.1.0"
    assert version_in("quote-service-staging-00001-abc") == "unknown"
