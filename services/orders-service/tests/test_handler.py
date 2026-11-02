import datetime
import io
import json

import handler as app
from conftest import request


def test_health_reports_version_and_environment():
    response = request("/health")
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["environment"] == "staging"


def test_page_shows_service_version_and_deploy_time(flags):
    response = request("/")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "orders-service" in response["body"]
    assert app.VERSION in response["body"]
    assert "26 Oct 2026, 09:30 UTC" in response["body"]


def test_page_shows_delivery_estimate_with_the_flag_off(flags):
    body = request("/")["body"]
    assert "Delivery estimate: 2 days</p>" in body and " with " not in body


def test_page_shows_the_carrier_with_the_flag_on(flags):
    flags["delivery_estimate_v2"] = {"enabled": True}
    assert "Delivery estimate: 2 days with Polder Post</p>" in request("/")["body"]


def test_estimate_api_is_hidden_with_the_flag_off(flags):
    assert request("/api/estimate", query={"store": "Utrecht Centraal"})["statusCode"] == 404


def test_estimate_api_answers_per_store_with_the_flag_on(flags):
    flags["delivery_estimate_v2"] = {"enabled": True}
    body = json.loads(request("/api/estimate", query={"store": "Amsterdam Zuid"})["body"])
    assert body == {"store": "Amsterdam Zuid", "carrier": "Grachtkoerier", "days": 1}
    assert json.loads(request("/api/estimate")["body"])["store"] == "Utrecht Centraal"
    assert request("/api/estimate", query={"store": "Atlantis"})["statusCode"] == 404


def test_a_missing_flag_or_no_answer_means_off(monkeypatch, capsys):
    def no_answer():
        raise TimeoutError("no answer")

    monkeypatch.setattr(app, "_poll_flags", no_answer)
    assert app.flag("delivery_estimate_v2") is False
    assert "flags: no answer from AWS AppConfig (TimeoutError), every flag off" in capsys.readouterr().out
    monkeypatch.setattr(app, "_poll_flags", lambda: app._flags.update(values={}))
    app._flags["next_poll"] = 0.0
    assert app.flag("delivery_estimate_v2") is False


class FakeAppConfigData:
    def __init__(self, contents):
        self.contents, self.sessions, self.polls = list(contents), [], 0

    def start_configuration_session(self, **kwargs):
        self.sessions.append(kwargs)
        return {"InitialConfigurationToken": "token-0"}

    def get_latest_configuration(self, ConfigurationToken):
        self.polls += 1
        content = self.contents.pop(0) if self.contents else b""
        return {"NextPollConfigurationToken": f"token-{self.polls}", "Configuration": io.BytesIO(content)}


def test_flags_come_from_one_session_polled_at_most_every_15_seconds(monkeypatch):
    client = FakeAppConfigData([b'{"delivery_estimate_v2": {"enabled": true}}', b""])
    app._flags["client"] = client
    for name, value in (("APPLICATION", "orders-service"), ("ENVIRONMENT", "production"), ("PROFILE", "flags")):
        monkeypatch.setenv(f"APPCONFIG_{name}", value)
    clock = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    assert app.flag("delivery_estimate_v2") is True
    clock[0] += 5
    assert app.flag("delivery_estimate_v2") is True and client.polls == 1
    clock[0] += 15
    assert app.flag("delivery_estimate_v2") is True and client.polls == 2  # unchanged: an empty answer
    assert client.sessions == [{"ApplicationIdentifier": "orders-service", "EnvironmentIdentifier": "production",
                                "ConfigurationProfileIdentifier": "flags", "RequiredMinimumPollIntervalInSeconds": 15}]
    assert app._flags["token"] == "token-2"


def test_without_appconfig_settings_every_flag_is_off(monkeypatch):
    for name in ("APPLICATION", "ENVIRONMENT", "PROFILE"):
        monkeypatch.delenv(f"APPCONFIG_{name}", raising=False)
    assert app.flag("delivery_estimate_v2") is False


def test_orders_carry_their_item_count():
    orders = app.todays_orders(datetime.date(2026, 10, 26))["orders"]
    assert all(order["item_count"] == len(order["items"]) for order in orders)


def test_totals_have_a_thousands_separator():
    assert app.euros(1234.5) == "1,234.50" and app.euros(89.95) == "89.95"


def test_page_escapes_environment_name(monkeypatch, flags):
    monkeypatch.setenv("ENVIRONMENT", "<staging>")
    assert "<staging>" not in request("/")["body"]


def test_orders_are_the_same_all_day():
    day = datetime.date(2026, 10, 26)
    first, again = app.todays_orders(day), app.todays_orders(day)
    assert first == again
    assert app.todays_orders(day) != app.todays_orders(day + datetime.timedelta(days=1))


def test_orders_api_returns_todays_orders():
    body = json.loads(request("/api/orders")["body"])
    assert body["date"] == datetime.date.today().isoformat()
    assert 8 <= len(body["orders"]) <= 14
    assert all(order["total_eur"] > 0 for order in body["orders"])


def test_unknown_path_and_method(flags):
    assert request("/admin")["statusCode"] == 404
    assert request("/", method="POST")["statusCode"] == 405


def test_commit_comes_from_build_info(tmp_path):
    (tmp_path / "VERSION").write_text("9.9.9\n")
    assert app.read_package(tmp_path) == ("9.9.9", "local")
    (tmp_path / "build_info.json").write_text('{"commit": "abc1234def"}')
    assert app.read_package(tmp_path) == ("9.9.9", "abc1234def")


def test_deploy_time_unknown_outside_lambda(monkeypatch):
    monkeypatch.delenv("DEPLOYED_AT")
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert app.deployed_at() == "unknown"
