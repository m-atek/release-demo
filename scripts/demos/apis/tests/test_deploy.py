import deploy
import pricing
import run_state
from conftest import FakeLambda


def test_back_takes_every_weight_off_and_the_alarms_away(monkeypatch):
    lam = FakeLambda(main="1", weights={"2": 0.1})
    removed = []
    monkeypatch.setattr(deploy, "remove_leftover_alarms", lambda: removed.append(True))
    monkeypatch.setattr(pricing, "api_url", lambda name: "https://api.example.invalid")
    deploys = {"production": {"number": "2", "previous_number": "1", "previous_version": "2.0.4",
                              "canary": {"state": "pulled-back"}}}
    deploy.go_back(lam, "pricing-api-production", deploys, "production", previous=False)
    assert (lam.main, lam.weights) == ("1", {})
    assert deploys["production"]["rolled_back_to"] == "2.0.4"
    assert removed


def test_route_sends_a_share_to_the_extra_version_only_when_asked():
    lam = FakeLambda()
    pricing.route(lam, "f", "1", "2", 0.1)
    assert (lam.main, lam.weights) == ("1", {"2": 0.1})
    pricing.route(lam, "f", "2")
    assert (lam.main, lam.weights) == ("2", {})
    assert pricing.routing(lam, "f") == ("2", {})


def test_a_production_deploy_publishes_without_traffic(monkeypatch, tmp_path):
    package = b"zip bytes"
    import hashlib

    run_state.save({"service": "pricing-api", "version": "2.1.1",
                    "package": {"path": "p/x.zip", "sha256": hashlib.sha256(package).hexdigest()}})

    class Publishing(FakeLambda):
        def update_function_code(self, **kwargs):
            self.code = kwargs["ZipFile"]

        def get_waiter(self, name):
            return type("W", (), {"wait": lambda self, **kw: None})()

        def publish_version(self, **kwargs):
            self.versions["3"] = kwargs["Description"]
            return {"Version": "3"}

    lam = Publishing(main="1", weights={"2": 0.1})
    monkeypatch.setattr(pricing, "lambda_client", lambda: lam)
    monkeypatch.setattr(pricing, "api_url", lambda name: "https://api.example.invalid")
    monkeypatch.setattr(pricing, "app_version", lambda client, function, number: "2.0.4")
    monkeypatch.setattr(deploy, "get_parameter", lambda client, name: "token")
    monkeypatch.setattr(deploy, "ssm_client", lambda: None)
    monkeypatch.setattr(pricing.shared("publish"), "download", lambda token, path: package)
    monkeypatch.setattr("sys.argv", ["deploy.py", "pricing-api", "production"])
    deploy.main()
    state = run_state.load()
    assert (lam.main, lam.weights) == ("1", {})          # live untouched, the leftover canary gone
    assert state["deploys"]["production"]["number"] == "3"
    assert state["deploys"]["production"]["canary"] == {"state": "ready"}
