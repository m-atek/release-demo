import datetime
import io
import json

import pytest

import handler as app
from conftest import request


def test_health_reports_version_and_environment(file):
    response = request("/health")
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["status"] == "ok"
    assert body["version"] == app.VERSION
    assert body["environment"] == "staging"


def test_page_shows_service_version_and_deploy_time(file):
    response = request("/")
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert "orders-service" in response["body"]
    assert app.VERSION in response["body"]
    assert "26 Oct 2026, 09:30 UTC" in response["body"]


def test_page_has_no_demo_label(file):
    assert "Demo system" not in request("/")["body"]


def test_page_shows_the_free_delivery_date_from_the_file(file):
    file["free_delivery_until"] = "2026-11-27"
    assert "Free delivery until 27 November" in request("/")["body"]


def test_without_a_setting_the_page_has_no_line(file):
    assert "Free delivery" not in request("/")["body"]


def test_a_date_that_does_not_exist_breaks_the_page_and_nothing_else(file):
    file["free_delivery_until"] = "2026-11-31"
    with pytest.raises(ValueError):
        request("/")
    assert request("/api/orders")["statusCode"] == 200
    assert request("/health")["statusCode"] == 200


def test_health_reports_what_the_file_said_even_when_the_page_breaks(file):
    file["free_delivery_until"] = "2026-11-31"
    assert json.loads(request("/health")["body"])["settings"] == {"free_delivery_until": "2026-11-31"}


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


def test_the_file_comes_from_one_session_polled_at_most_every_15_seconds(monkeypatch):
    client = FakeAppConfigData([b'{"free_delivery_until": "2026-11-27"}', b"", b'{"free_delivery_until": "2026-11-30"}'])
    app._settings["client"] = client
    clock = [100.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    assert app.settings() == {"free_delivery_until": "2026-11-27"}
    clock[0] += 5
    assert app.settings() == {"free_delivery_until": "2026-11-27"} and client.polls == 1
    clock[0] += 15
    assert app.settings() == {"free_delivery_until": "2026-11-27"} and client.polls == 2  # unchanged: an empty answer
    clock[0] += 15
    assert app.settings() == {"free_delivery_until": "2026-11-30"} and client.polls == 3
    assert client.sessions == [{"ApplicationIdentifier": "orders-service-settings", "EnvironmentIdentifier": "staging",
                                "ConfigurationProfileIdentifier": "settings", "RequiredMinimumPollIntervalInSeconds": 15}]
    assert app._settings["token"] == "token-3"


def test_no_answer_keeps_the_last_file_and_starts_a_new_session(monkeypatch):
    class Refusing(FakeAppConfigData):
        def get_latest_configuration(self, ConfigurationToken):
            raise RuntimeError("no answer")

    app._settings.update(client=Refusing([]), values={"free_delivery_until": "2026-11-27"})
    assert app.settings() == {"free_delivery_until": "2026-11-27"}
    assert app._settings["token"] is None


def test_outside_aws_nothing_is_asked(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local")
    assert app.settings() == {} and app._settings["client"] is None


def test_the_client_is_made_once_and_only_when_asked(monkeypatch):
    client = FakeAppConfigData([b'{"free_delivery_until": "2026-11-27"}'])
    made = []
    import types

    monkeypatch.setitem(__import__("sys").modules, "boto3", types.SimpleNamespace(client=lambda name: made.append(name) or client))
    assert app.settings() == {"free_delivery_until": "2026-11-27"}
    assert made == ["appconfigdata"]


def test_page_escapes_environment_name(monkeypatch, file):
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


def test_unknown_path_and_method():
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
