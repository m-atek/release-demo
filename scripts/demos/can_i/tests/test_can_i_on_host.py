"""The admin's steps on the tools host, with kubectl and k3s stubbed: what they ask for, and in what order."""

import json

import pytest

import on_host


class FakeK3s:
    """Answers kubectl and records every command. Pods stay for pod_rounds asks, then they are gone."""

    def __init__(self, tmp_path, monkeypatch, pod_rounds=1, labelled=True, audit=True):
        self.calls, self.pod_rounds, self.labelled = [], pod_rounds, labelled
        monkeypatch.setattr(on_host.k3s, "kubectl", self.kubectl)
        monkeypatch.setattr(on_host.k3s, "run", lambda *command, **kw: self.calls.append(command) or "")
        monkeypatch.setattr(on_host.k3s, "CONFIG_DIR", tmp_path / "rancher")
        monkeypatch.setattr(on_host.k3s, "AUDIT_POLICY", tmp_path / "server" / "audit.yaml")
        monkeypatch.setattr(on_host.k3s, "AUDIT_LOG", tmp_path / "server" / "logs" / "audit.log")
        monkeypatch.setattr(on_host, "active", lambda: True)
        monkeypatch.setattr(on_host, "image_on_node", lambda: False)
        monkeypatch.setattr(on_host.time, "sleep", lambda seconds: None)
        (tmp_path / "rancher").mkdir()
        (tmp_path / "server" / "logs").mkdir(parents=True)
        if audit:
            on_host.k3s.AUDIT_POLICY.write_text("policy")
            (tmp_path / "rancher" / "config.yaml").write_text(f"audit-policy-file={on_host.k3s.AUDIT_POLICY}\n")

    def kubectl(self, *args, check=True, stdin=None):
        self.calls.append(("kubectl", *args))
        if args[:2] == ("get", "namespace"):
            labels = {on_host.ENFORCE: "baseline", on_host.ENFORCE_VERSION: "v1.36"} if self.labelled else {}
            return json.dumps({"metadata": {"labels": labels}})
        if args[:2] == ("get", "pods"):
            self.pod_rounds -= 1
            return "pod/peek-1\n" if self.pod_rounds >= 0 else ""
        if args[0] == "apply":
            return "secret/catalog-db configured\n"
        return ""

    def index(self, *start):
        return next(i for i, call in enumerate(self.calls) if call[:len(start)] == start)


def test_prepare_goes_in_the_order_a_take_needs(tmp_path, monkeypatch, capsys):
    fake = FakeK3s(tmp_path, monkeypatch)
    on_host.prepare()
    deleted = [call for call in fake.calls if call[:2] == ("kubectl", "delete")]
    assert {call[call.index("-n") + 1] for call in deleted} == {"staging", "production"}
    assert all(call[2:5] == ("deployment", "peek", "hostpath-check") and "--ignore-not-found" in call for call in deleted)
    assert fake.index("kubectl", "delete") < fake.index("kubectl", "get", "pods") < fake.index("kubectl", "apply")
    assert fake.index("kubectl", "apply") < fake.index("kubectl", "label") < fake.index("k3s", "crictl", "pull")
    assert ("k3s", "crictl", "pull", "busybox:1.37") in fake.calls
    labelled = ("kubectl", "label", "role", "developer", "-n", "production", "release-demo/demo=can-i", "--overwrite")
    assert labelled in fake.calls
    printed = capsys.readouterr().out.splitlines()
    assert printed[-1].startswith("mark ") and printed[-1].endswith("Z")  # last: nothing a take shows comes after it


def test_prepare_doesnt_ask_docker_hub_for_an_image_the_node_has(tmp_path, monkeypatch):
    fake = FakeK3s(tmp_path, monkeypatch)
    monkeypatch.setattr(on_host, "image_on_node", lambda: True)
    on_host.prepare()
    assert not any(call[:2] == ("k3s", "crictl") for call in fake.calls)


def test_prepare_applies_the_clusters_files_and_the_role_as_the_take_starts(tmp_path, monkeypatch):
    fake = FakeK3s(tmp_path, monkeypatch)
    on_host.prepare()
    applied = next(call for call in fake.calls if call[:2] == ("kubectl", "apply"))
    assert applied[2:] == ("-f", str(on_host.HERE / "cluster"), "-f", str(on_host.HERE / "take" / "before.yaml"))


def test_prepare_waits_until_the_pods_are_gone(tmp_path, monkeypatch):
    """busybox's sh ignores SIGTERM, so a pod takes 30 s. Its delete events must come before the mark."""
    fake = FakeK3s(tmp_path, monkeypatch, pod_rounds=5)
    on_host.prepare()
    asks = [i for i, call in enumerate(fake.calls) if call[:3] == ("kubectl", "get", "pods")]
    assert len(asks) >= 6 and asks[-1] < fake.index("kubectl", "apply")


def test_prepare_stops_if_a_pod_wont_go(tmp_path, monkeypatch):
    FakeK3s(tmp_path, monkeypatch, pod_rounds=10**6)
    monkeypatch.setattr(on_host, "GONE_SECONDS", 0)
    with pytest.raises(SystemExit, match="still there"):
        on_host.prepare()


@pytest.mark.parametrize("settings, message", [({"audit": False}, "audit log isn't on"),
                                               ({"labelled": False}, "no Pod Security label")])
def test_prepare_refuses_a_cluster_that_isnt_set_up(tmp_path, monkeypatch, settings, message):
    fake = FakeK3s(tmp_path, monkeypatch, **settings)
    with pytest.raises(SystemExit, match=message):
        on_host.prepare()
    assert not [call for call in fake.calls if call[:2] == ("kubectl", "delete")]


@pytest.mark.parametrize("switch, settings", [("NEEDS_AUDIT_LOG", {"audit": False}),
                                               ("NEEDS_POD_SECURITY", {"labelled": False})])
def test_decision_72s_defaults_each_have_one_switch(tmp_path, monkeypatch, capsys, switch, settings):
    FakeK3s(tmp_path, monkeypatch, **settings)
    monkeypatch.setattr(on_host, switch, False)
    on_host.prepare()
    assert capsys.readouterr().out.splitlines()[-1].startswith("mark ")


def test_remove_deletes_by_the_label_and_peek_only(tmp_path, monkeypatch):
    fake = FakeK3s(tmp_path, monkeypatch)
    on_host.remove()
    deleted = [call[2:] for call in fake.calls if call[:2] == ("kubectl", "delete")]
    assert deleted == [
        ("secret,role,rolebinding,deployment", "-n", "staging", "-l", "release-demo/demo=can-i", "--ignore-not-found"),
        ("deployment", "peek", "-n", "staging", "--ignore-not-found"),
        ("secret,role,rolebinding,deployment", "-n", "production", "-l", "release-demo/demo=can-i", "--ignore-not-found"),
        ("deployment", "peek", "-n", "production", "--ignore-not-found")]
    assert not any("--all" in call or "namespace" in call for call in fake.calls)


def test_remove_with_k3s_stopped_touches_nothing(tmp_path, monkeypatch, capsys):
    fake = FakeK3s(tmp_path, monkeypatch)
    monkeypatch.setattr(on_host, "active", lambda: False)
    on_host.remove()
    assert fake.calls == [] and "nothing to remove" in capsys.readouterr().out


@pytest.mark.parametrize("rules, state", [
    ([{"resources": ["deployments", "replicasets"], "verbs": ["get", "list", "watch"]},
      {"resources": ["pods", "pods/log", "events"], "verbs": ["get", "list", "watch"]},
      {"resources": ["deployments"], "verbs": ["create", "update", "patch", "delete"]}], "before"),
    ([{"resources": ["deployments", "replicasets"], "verbs": ["get", "list", "watch"]}], "after"),
    ([{"resources": ["deployments"], "verbs": ["get", "patch"]}], "other"),
    ([{"resources": ["deployments"], "verbs": ["create", "update", "patch", "delete", "deletecollection"]}], "other"),
])
def test_the_roles_state(rules, state):
    assert on_host.role_state(rules).startswith(state)


def test_status_says_what_it_should_and_asks_for_secrets_by_name_only(tmp_path, monkeypatch, capsys):
    fake = FakeK3s(tmp_path, monkeypatch)
    usual = fake.kubectl

    def kubectl(*args, check=True, stdin=None):
        if args[:2] == ("get", "role"):
            fake.calls.append(("kubectl", *args))
            return json.dumps({"rules": [{"resources": ["deployments"], "verbs": ["get"]}]})
        if args[:2] == ("get", "secret"):
            fake.calls.append(("kubectl", *args))
            return "secret/catalog-db\n"
        return usual(*args, check=check, stdin=stdin)

    monkeypatch.setattr(on_host.k3s, "kubectl", kubectl)
    on_host.k3s.AUDIT_LOG.write_text("x" * (1 << 20))
    on_host.status()
    out = capsys.readouterr().out
    assert "role developer in production: after (read only)" in out
    assert "fake Secret catalog-db in: staging, production" in out
    assert "audit log: on, 1.0 MB of at most 250 MB" in out
    assert "Pod Security in staging: baseline v1.36" in out and "Pod Security in production: baseline v1.36" in out
    secret_calls = [call for call in fake.calls if call[:3] == ("kubectl", "get", "secret")]
    assert len(secret_calls) == 2 and all(call[call.index("-o") + 1] == "name" for call in secret_calls)


def test_no_step_ever_asks_for_a_secrets_body():
    text = (on_host.HERE / "on_host.py").read_text()
    assert '"secret", "catalog-db", "-n", namespace, "-o", "name"' in text
    assert "jsonpath" not in text and "get\", \"secrets" not in text


def event(user, resource, code, **extra):
    return json.dumps({"user": {"username": user}, "objectRef": {"resource": resource}, "responseStatus": {"code": code},
                       **extra}) + "\n"


def test_the_raw_log_counts_are_zero_on_a_clean_log():
    lines = [event("sam", "secrets", 403), event("system:node:tools-host", "secrets", 200),
             event("sam", "pods", 200), "not json\n"]
    assert on_host.log_counts(lines, ["fake-a", "fake-b"]) == {"sam_read_a_secret": 0, "body_on_a_secret": 0,
                                                              "lines_with_a_fake_value": 0}


def test_the_raw_log_counts_catch_each_thing_the_spec_forbids():
    counts = on_host.log_counts([event("sam", "secrets", 200), event("sam", "secrets", 200),
                                 event("x", "secrets", 200, requestObject={"a": "b"}),
                                 event("x", "secrets", 200, responseObject={"a": "b"}),
                                 event("x", "pods", 201, responseObject={"note": "fake-a"})], ["fake-a", "fake-b"])
    assert counts == {"sam_read_a_secret": 2, "body_on_a_secret": 2, "lines_with_a_fake_value": 1}


def test_log_check_prints_counts_and_never_a_line(tmp_path, monkeypatch, capsys):
    FakeK3s(tmp_path, monkeypatch)
    value = "demo-value-not-a-real-password"
    on_host.k3s.AUDIT_LOG.write_text(event("x", "pods", 201, responseObject={"note": value}))
    on_host.log_check()
    out = capsys.readouterr().out
    assert json.loads(out)["lines_with_a_fake_value"] == 1 and value not in out
