"""What the audit-log prints on screen: the log's own fields, two lines an event, at most 56 columns."""

import json

import pytest

import remote
import who

MARK = "2026-11-02T13:21:50Z"
CONTROLLER = "system:serviceaccount:kube-system:replicaset-controller"
PIPELINE = "system:serviceaccount:jenkins:release-pipeline"


def row(time, code, verb, resource, name, user, subresource=None):
    return {"time": f"2026-11-02T13:{time}.123456Z", "code": code, "verb": verb, "resource": resource,
            "subresource": subresource, "name": name, "namespace": "production", "user": user}


TAKE = [row("22:04", 403, "get", "secrets", "catalog-db", "sam"),
        row("22:11", 201, "create", "deployments", "peek", "sam"),
        row("22:11", 201, "create", "pods", "peek-5b7c9d6f4-x2x9q", CONTROLLER),
        row("22:12", 200, "watch", "secrets", "catalog-db", "system:node:tools-host"),
        row("22:19", 200, "get", "pods", "peek-5b7c9d6f4-x2x9q", "sam", "log")]


def test_the_take_prints_five_events_in_ten_lines():
    assert who.lines(who.keep(TAKE, "peek")) == [
        "14:22:04  403  get secrets/catalog-db",
        "    by sam",
        "14:22:11  201  create deployments/peek",
        "    by sam",
        "14:22:11  201  create pods/peek-5b7c9d6f4-x2x9q",
        "    by kube-system:replicaset-controller",
        "14:22:12  200  watch secrets/catalog-db",
        "    by system:node:tools-host",
        "14:22:19  200  get pods/peek-5b7c9d6f4-x2x9q/log",
        "    by sam"]


def test_every_line_fits_the_terminal_with_the_longest_names_it_can_print():
    rows = [*TAKE, row("30:01", 200, "patch", "deployments", "catalog", PIPELINE),
            row("30:02", 201, "create", "pods", "catalog-6c9f7b5d44-abcde", CONTROLLER),
            row("30:03", 200, "get", "pods", "catalog-6c9f7b5d44-abcde", "sam", "log"),
            row("30:04", 403, "patch", "deployments", "catalog", "sam", "scale")]
    printed = who.lines(rows)
    assert max(len(line) for line in printed) <= 56


def test_it_prints_summer_time_too():
    assert who.clock("2026-07-02T12:22:04.1Z") == "14:22:04"
    assert who.clock("2026-11-02T13:22:04.123456Z") == "14:22:04"


def test_it_shortens_only_kube_systems_prefix():
    assert who.user({"user": CONTROLLER}) == "kube-system:replicaset-controller"
    assert who.user({"user": PIPELINE}) == PIPELINE  # the account the video names, whole
    assert who.user({"user": "system:node:tools-host"}) == "system:node:tools-host"


def test_it_prints_no_other_field():
    extra = {**TAKE[0], "requestObject": {"stringData": {"password": "LEAKED"}}, "responseObject": {"x": "LEAKED"},
             "sourceIPs": ["LEAKED"], "annotations": {"reason": "LEAKED"}, "userAgent": "LEAKED"}
    assert "LEAKED" not in "\n".join(who.lines([extra]))


def test_it_keeps_the_first_event_per_user_verb_and_object():
    again = [*TAKE, row("22:40", 200, "get", "pods", "peek-5b7c9d6f4-x2x9q", "sam", "log")]
    started = row("22:12", 200, "watch", "secrets", "catalog-db", "system:node:tools-host")
    complete = {**started, "time": "2026-11-02T13:25:12.123456Z"}
    kept = who.keep([*again, complete], "peek")
    assert len(kept) == 5
    assert kept[3]["time"] == started["time"] and kept[4]["time"] == TAKE[4]["time"]


def test_another_verb_is_another_event():
    both = [row("22:12", 200, "list", "secrets", "catalog-db", "system:node:tools-host"),
            row("22:12", 200, "watch", "secrets", "catalog-db", "system:node:tools-host")]
    assert len(who.keep(both, "peek")) == 2


def test_events_come_in_the_order_they_were_asked():
    assert [r["code"] for r in who.keep(list(reversed(TAKE)), "peek")] == [403, 201, 201, 200, 200]


def test_for_catalog_only_writes_to_the_deployment():
    rows = [row("30:00", 200, "patch", "deployments", "catalog", PIPELINE),
            row("30:01", 201, "create", "pods", "catalog-6c9f7b5d44-abcde", CONTROLLER),
            row("30:02", 200, "delete", "pods", "catalog-5b7c9d6f4-zzzzz", "system:node:tools-host"),
            row("30:03", 200, "watch", "secrets", "catalog-db", "system:node:tools-host"),
            row("30:04", 200, "get", "pods", "catalog-6c9f7b5d44-abcde", "sam", "log")]
    assert who.lines(who.keep(rows, "catalog")) == ["14:30:00  200  patch deployments/catalog",
                                                    f"    by {PIPELINE}"]


def test_the_mark_is_never_a_guess(tmp_path, capsys):
    with pytest.raises(SystemExit):
        who.read_mark(tmp_path / "missing")
    message = capsys.readouterr().err
    assert "Run the reset" in message and str(tmp_path) not in message


def test_the_mark_goes_in_a_private_folder(tmp_path):
    path = tmp_path / "kube" / "can-i-mark"
    who.store_mark(MARK, path)
    assert who.read_mark(path) == MARK
    assert oct(path.parent.stat().st_mode)[-3:] == "700"


def test_it_asks_for_production_since_the_mark(monkeypatch):
    asked = []
    output = "\n".join(json.dumps(r) for r in TAKE) + "\n"
    monkeypatch.setattr(remote, "run", lambda command, timeout: asked.append(command) or (0, output))
    assert who.events("peek", MARK) == TAKE
    assert asked == [f"{remote.K3S} audit --since {MARK} --namespace production --name peek"]


def test_a_failed_read_says_so(monkeypatch, capsys):
    monkeypatch.setattr(remote, "run", lambda command, timeout: (1, "k3s isn't running"))
    with pytest.raises(SystemExit):
        who.events("peek", MARK)
    assert "didn't answer" in capsys.readouterr().err
