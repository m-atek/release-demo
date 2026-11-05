"""The first-day check on a fake cluster, through every way the day can go. The fake follows what the source at
v1.36.5 says, with switches for each thing that could turn out otherwise."""

import json
import re

import pytest
import yaml

import check


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Fake:
    """The API server, as far as this check asks it. Each request costs a little time."""

    def __init__(self, clock, **reality):
        self.clock = clock
        self.reality = {"scale_matches_subresource": True, "star_matches": True, "omits_zero": True, "dry_runs_only": False,
                        "server_refuses_anyway": False, "serves_api": True, "answers": True, "audit": True,
                        "others": [], "calls_fail": True, "event": True, "conflict_at_three": False, **reality}
        self.policies = {}
        self.bindings = set()
        self.wanted = 3
        self.pod_names = {"catalog-a": 0, "catalog-b": 0, "catalog-c": 0}
        self.context = "staging"
        self.requests = []
        self.real_scale_at = None
        self.first_failed = self.last_failed = None
        self.back_at = None

    # The rules, as rules.go and validator.go read.

    def matches(self, resources, subresource):
        for item in resources:
            name, _, sub = item.partition("/")
            if subresource == "scale" and sub == "scale" and name == "deployments":
                return self.reality["scale_matches_subresource"]
            if subresource == "scale" and sub == "scale" and name == "*":
                return self.reality["star_matches"]
            if name == "deployments" and sub == subresource:
                return True
        return False

    def judge(self, namespace, subresource, replicas):
        """The refusal's words, or None. Only production has a binding."""
        if self.reality["server_refuses_anyway"]:
            return 'Error from server (Forbidden): something else says no'
        if namespace != "production":
            return None
        for name, document in self.policies.items():
            if name not in self.bindings:
                continue
            rule = document["spec"]["matchConstraints"]["resourceRules"][0]
            if not self.matches(rule["resources"], subresource):
                continue
            validation = document["spec"]["validations"][0]
            expression = validation["expression"]
            guarded = "orValue" in expression or "has(" in expression
            if replicas == 0 and subresource == "scale" and self.reality["omits_zero"] and not guarded:
                return (f'The deployments "catalog" is invalid: : ValidatingAdmissionPolicy \'{name}\' with binding \'{name}\' '
                        f"denied request: expression '{expression}' resulted in error: no such key: replicas")
            if replicas == 0:
                return (f'Error from server ({validation["reason"]}): deployments.apps "catalog" is forbidden: '
                        f"ValidatingAdmissionPolicy '{name}' with binding '{name}' denied request: {validation['message']}")
        return None

    def namespace_of(self, args):
        if "--context" in args:
            return args[args.index("--context") + 1]
        return self.context

    def kubectl(self, *args, stdin=None, timeout=60):
        self.clock.now += 0.3
        self.requests.append(args)
        if not self.reality["answers"]:
            return check.Reply(1, "Unable to connect to the server: dial tcp 127.0.0.1:16443: connect: connection refused")
        namespace = self.namespace_of(args)
        words = [word for word in args if not word.startswith("--context")]
        if "--context" in args:
            words.remove(namespace)
        verb = words[0]
        dry = "--dry-run=server" in words
        if verb == "version":
            return check.Reply(0, json.dumps({"clientVersion": {"gitVersion": "v1.34.1"}, "serverVersion": {"gitVersion": "v1.36.5+k3s1"}}))
        if verb == "api-resources":
            return check.Reply(0, "validatingadmissionpolicies.admissionregistration.k8s.io" if self.reality["serves_api"] else "")
        if verb == "config":
            self.context = words[2]
            return check.Reply(0, f'Switched to context "{words[2]}".')
        if verb == "apply" and "--server-side" in words:
            if self.wanted == 0 or self.reality["conflict_at_three"]:
                return check.Reply(1, 'Apply failed with 1 conflict: conflict with "kubectl" using apps/v1: .spec.replicas')
            return check.Reply(0, "deployment.apps/catalog serverside-applied (server dry run)")
        if verb == "apply":
            text = stdin if stdin is not None else open(words[words.index("-f") + 1]).read()
            for document in yaml.safe_load_all(text):
                name = document["metadata"]["name"]
                if document["kind"] == "ValidatingAdmissionPolicy":
                    self.policies[name] = document
                else:
                    self.bindings.add(name)
            return check.Reply(0, "created")
        if verb == "delete" and words[1] in ("validatingadmissionpolicy", "validatingadmissionpolicybinding"):
            if not dry:
                self.policies.pop(words[2], None) if words[1] == "validatingadmissionpolicy" else self.bindings.discard(words[2])
            return check.Reply(0, "deleted")
        if verb == "delete":
            return check.Reply(0, "deleted (server dry run)")
        if verb == "patch":
            refused = self.judge(namespace, "", 0)
            return check.Reply(1, refused) if refused else check.Reply(0, "patched (server dry run)")
        if verb == "scale":
            count = int(next(word for word in words if word.startswith("--replicas=")).split("=")[1])
            refused = self.judge(namespace, "scale", count)
            if self.reality["dry_runs_only"] and not dry:
                refused = None
            if refused:
                return check.Reply(1, refused)
            if not dry and namespace == "production":
                if count == 0:
                    self.wanted, self.real_scale_at = 0, self.clock.now
                    self.pods_gone_at = self.clock.now + 8
                    if self.reality["calls_fail"]:
                        self.first_failed = self.clock.now + 6
                else:
                    self.wanted, self.back_at = count, self.clock.now + 7
                    if self.reality["calls_fail"]:
                        self.last_failed = self.clock.now + 6
                return check.Reply(0, "deployment.apps/catalog scaled")
            return check.Reply(0, "deployment.apps/catalog scaled (server dry run)")
        if verb == "get":
            return self.get(words, namespace)
        if verb == "describe":
            line = "Normal ScalingReplicaSet 9s deployment-controller Scaled down replica set catalog-5d8 from 3 to 0"
            return check.Reply(0, f"Name: catalog\nEvents:\n  {line}\n" if self.reality["event"] else "Name: catalog\nEvents: <none>\n")
        raise AssertionError(f"not expected: {args}")

    def get(self, words, namespace):
        if words[1].startswith("deploy,"):
            return check.Reply(0, "\n".join(["deployment.apps/catalog", *self.reality["others"]]))
        if words[1] == "deploy":
            ready = self.wanted if self.back_at is None or self.clock.now >= self.back_at else 0
            if self.wanted == 0:
                ready = 0
            return check.Reply(0, json.dumps({"spec": {"replicas": self.wanted}, "status": {"readyReplicas": ready} if ready else {}}))
        if words[1] == "pods" and "-o" in words:
            pods = self.pod_names if self.wanted else {}
            return check.Reply(0, json.dumps({"items": [{"metadata": {"name": name}, "status": {"containerStatuses": [{"restartCount": count}]}}
                                                        for name, count in pods.items()]}))
        if words[1] == "pods":
            if self.wanted == 0 and self.clock.now >= self.pods_gone_at:
                return check.Reply(0, "No resources found in production namespace.")
            return check.Reply(0, "NAME READY STATUS\ncatalog-a 1/1 Terminating")
        raise AssertionError(f"not expected: {words}")

    def host(self, command, timeout=120):
        self.clock.now += 1
        if "k3s.py audit" in command:
            if not self.reality["audit"]:
                return check.Reply(0, "")
            row = {"time": "x", "code": 403, "verb": "patch", "resource": "deployments", "subresource": "scale", "name": "catalog",
                   "namespace": "production", "user": "presenter"}
            return check.Reply(0, json.dumps(row) + "\n")
        if "kubectl" in command:  # k3s's admin, the same command as a dry run
            assert "--dry-run=server" in command and command.startswith(f"KUBECONFIG={check.ADMIN}")
            refused = self.judge("production", "scale", 0)
            return check.Reply(1, refused) if refused else check.Reply(0, "scaled (server dry run)")
        raise AssertionError(f"not expected on the host: {command}")

    def calls(self, seconds):
        assert seconds == check.CALLS_SECONDS
        fake = self

        class Handle:
            def result(self):
                said = "Answering again: 87 calls failed.\n" if fake.first_failed else ""
                summary = {"answered": 900, "failed": 87 if fake.first_failed else 0, "first_failed": fake.first_failed,
                           "last_failed": fake.last_failed}
                return check.Reply(0, f"TIME ANSWERED FAILED\n{said}== summary {json.dumps(summary)}\n")

        return Handle()


def run(**reality):
    clock = Clock()
    cluster = Fake(clock, **reality)
    lines = []
    code = check.Check(cluster, out=lines.append, clock=clock.time, sleep=clock.sleep).run()
    return code, "\n".join(lines), cluster


def assert_clean(cluster):
    assert cluster.policies == {} and cluster.bindings == set(), "it left a policy behind"
    assert cluster.wanted == 3, "it left catalog short"
    assert cluster.context == "staging"


def test_as_the_source_says_the_video_stands():
    code, text, cluster = run()
    assert code == 0, text
    assert "THE VIDEO STANDS." in text
    assert "FAIL" not in text
    assert_clean(cluster)


def test_the_fills_for_the_brackets_are_printed():
    _, text, _ = run()
    for words in ("the policy is live after the apply", "Enter to the refusal", "Enter to the first failed call", "Enter to no pods",
                  "Enter to the last failed call", "Enter to all ready", "failed calls: 87"):
        assert words in text


def test_the_wrong_versions_are_reported_and_decide_nothing():
    _, text, _ = run()
    assert "a rule with only deployments: a patch to zero refused, kubectl scale passes" in text
    assert "no such key: replicas" in text
    assert "a rule with only deployments lets kubectl scale past: True" in text


def test_the_real_command_goes_without_a_dry_run_and_without_a_namespace():
    _, _, cluster = run()
    plain = [request for request in cluster.requests if request == check.THE_COMMAND]
    assert len(plain) == 2  # once refused, once for real with the policy off


def test_with_the_policy_on_only_dry_runs_could_have_scaled_anything():
    _, text, cluster = run()
    # The refused real command moved nothing: the same pods, the same restarts.
    assert "no pod moved" in text


def test_it_scales_for_real_only_with_the_policy_off():
    code, text, cluster = run()
    assert "5. before the real run: 3 of 3 ready, policy off" in text
    assert code == 0


def test_if_deployments_scale_doesnt_catch_it_but_every_scale_does_one_word_changes():
    code, text, cluster = run(scale_matches_subresource=False)
    assert code == 2
    assert "*/scale catches it" in text and "NOT refused" in text
    assert_clean(cluster)


def test_if_nothing_catches_it_the_video_doesnt_stand_and_no_other_command_goes_in_its_place():
    code, text, cluster = run(scale_matches_subresource=False, star_matches=False)
    assert code == 1
    assert "THE VIDEO DOESN'T STAND" in text and "Don't film kubectl patch" in text
    assert_clean(cluster)


def test_if_the_dry_run_is_refused_and_the_real_command_isnt_the_video_doesnt_stand():
    code, text, cluster = run(dry_runs_only=True)
    assert code == 1
    assert "the dry run was refused, and the real command wasn't" in text
    assert_clean(cluster)


def test_a_refusal_in_other_words_is_a_fix_not_a_film():
    original = check.POLICY_FILE.read_text()
    document = original.replace("object.spec.?replicas.orValue(0) > 0", "object.spec.replicas > 0")
    path = check.POLICY_FILE.with_name("test-no-guard.yaml")
    path.write_text(document)
    try:
        check.POLICY_FILE, saved = path, check.POLICY_FILE
        code, text, cluster = run()
    finally:
        check.POLICY_FILE = saved
        path.unlink()
    assert code == 2
    assert "not in the policy's own words" in text and "has(object.spec.replicas)" in text
    assert_clean(cluster)


def test_a_file_the_api_server_wont_take_is_a_fix_with_the_other_form_named(monkeypatch):
    clock = Clock()
    cluster = Fake(clock)
    original = cluster.kubectl

    def kubectl(*args, stdin=None, timeout=60):
        if "-f" in args and args[args.index("-f") + 1] == str(check.POLICY_FILE):
            return check.Reply(1, 'The ValidatingAdmissionPolicy "no-zero" is invalid: compilation failed')
        return original(*args, stdin=stdin, timeout=timeout)

    cluster.kubectl = kubectl
    lines = []
    code = check.Check(cluster, out=lines.append, clock=clock.time, sleep=clock.sleep).run()
    text = "\n".join(lines)
    assert code == 2 and "didn't accept no-zero.yaml" in text and "has(object.spec.replicas)" in text
    assert_clean(cluster)


def test_a_warning_on_stderr_doesnt_break_what_it_reads_as_json():
    clock = Clock()
    cluster = Fake(clock)
    original = cluster.kubectl

    def kubectl(*args, stdin=None, timeout=60):
        reply = original(*args, stdin=stdin, timeout=timeout)
        if "version" in args:
            return check.Reply(reply.code, reply.text + "\nWARNING: version difference between client and server", reply.text)
        return reply

    cluster.kubectl = kubectl
    assert check.Check(cluster, out=lambda line: None, clock=clock.time, sleep=clock.sleep).run() == 0


def test_refused_with_no_policy_proves_nothing():
    code, text, cluster = run(server_refuses_anyway=True)
    assert code == 2
    assert "refused with no policy on" in text
    assert_clean(cluster)


def test_a_cluster_that_doesnt_serve_the_api_doesnt_stand():
    code, text, _ = run(serves_api=False)
    assert code == 1 and "doesn't serve the policy" in text


def test_no_answer_says_connect_py_and_gives_no_verdict_on_the_policy():
    clock = Clock()
    cluster = Fake(clock, answers=False)
    lines = []
    code = check.Check(cluster, out=lines.append, clock=clock.time, sleep=clock.sleep).run()
    text = "\n".join(lines)
    assert code == 2 and "connect.py" in text


def test_something_else_in_production_is_named_and_left_alone():
    code, text, cluster = run(others=["statefulset.apps/catalog-postgres"])
    assert code == 2 and "catalog-postgres" in text
    assert not [r for r in cluster.requests if "scale" in r and "--dry-run=server" not in r]


def test_no_audit_row_holds_the_video_until_its_read():
    code, text, _ = run(audit=False)
    assert code == 2 and "THE POLICY HOLDS" in text and "the audit log" in text


def test_calls_that_never_fail_mean_a_fix_before_a_take():
    code, text, _ = run(calls_fail=False)
    assert code == 2 and "THE POLICY HOLDS" in text and "the calls: 0 of" in text


def test_no_event_means_a_fix_before_a_take():
    code, text, _ = run(event=False)
    assert code == 2 and "the events still say from None" in text


def test_a_conflict_after_the_scale_back_would_stop_the_release():
    code, text, _ = run(conflict_at_three=True)
    assert code == 2 and "an apply of 3 replicas now: Apply failed" in text


def test_it_cleans_up_even_if_something_breaks_in_the_middle():
    clock = Clock()
    cluster = Fake(clock)
    original = cluster.calls

    def broken(seconds):
        raise RuntimeError("no session")

    cluster.calls = broken
    with pytest.raises(RuntimeError):
        check.Check(cluster, out=lambda line: None, clock=clock.time, sleep=clock.sleep).run()
    assert_clean(cluster)
    assert original  # the fake itself still works


def test_it_deletes_only_policies_it_made_or_the_video_applies():
    _, _, cluster = run()
    deletes = {request[3:5] for request in cluster.requests if "delete" in request and "--dry-run=server" not in request}
    assert deletes <= {("validatingadmissionpolicybinding", check.POLICY), ("validatingadmissionpolicy", check.POLICY),
                       ("validatingadmissionpolicybinding", check.CHECK_POLICY), ("validatingadmissionpolicy", check.CHECK_POLICY)}


def test_it_applies_the_real_file_not_a_copy():
    _, _, cluster = run()
    assert any(request[-2:] == ("-f", str(check.POLICY_FILE)) for request in cluster.requests)


def test_the_exit_codes_say_what_the_docstring_says():
    text = check.__doc__
    assert re.search(r"0\s+THE VIDEO STANDS", text) and re.search(r"1\s+THE VIDEO DOESN'T STAND", text) and re.search(r"2\s+NOT YET", text)
