"""Video 10: rotate.py and its rotators, the secrets inventory, and the
take's own scripts, against fakes of Slack, JFrog, SSM, Jenkins and
ServiceNow. No token is revoked, made or stored: every one here is built in
the test, and none is real.

    .venv/bin/python -m pytest -q scripts/rotators/tests
"""

import base64
import datetime
import io
import json
import subprocess
from zoneinfo import ZoneInfo

import pytest

import inventory
import rotate
import rotators
from rotators import Failed, Steps, jcr, records, sonar

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
SUBJECT = "jfac@01jcr0000000000000000000/users/release-pipeline"


def fake_token(jti, sub=SUBJECT, iat=1_793_000_000):
    """A token shaped like JFrog's, made here: base64url JSON, and a signature of a's."""
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return ".".join([encode({"typ": "JWT", "alg": "RS256", "kid": "test0000000000"}),
                     encode({"sub": sub, "scp": "applied-permissions/user", "aud": "*@*", "iat": iat,
                             "exp": iat + 86400, "jti": jti}), "a" * 43])


def at(hh, mm, ss):
    return datetime.datetime(2026, 11, 2, hh, mm, ss, tzinfo=AMSTERDAM).timestamp()


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeJFrog:
    def __init__(self, clock, tokens, rejects_after=0.0, revoke_status=200):
        self.clock, self.live = clock, dict(tokens)  # jti: value
        self.rejects_after, self.revoke_status = rejects_after, revoke_status
        self.revoked_at, self.calls, self.issued = {}, [], None

    def user_tokens(self):
        return [{"token_id": jti, "subject": SUBJECT} for jti in self.live]

    def revoke(self, token_id, value=None):
        self.calls.append(("revoke", token_id, value is not None))
        if self.revoke_status == 404 and value:
            self.calls.append(("delete-me", token_id))
        elif self.revoke_status != 200:
            return self.revoke_status
        self.revoked_at[token_id] = self.clock()
        self.live.pop(token_id, None)
        return 200

    def issue(self):
        self.calls.append(("issue",))
        self.issued = fake_token("2c91d0b4-0000-4000-8000-000000000002")
        return self.issued

    def answer(self, token):
        jti = rotators.jwt_claims(token)["jti"]
        if jti in self.revoked_at:
            return 401 if self.clock() - self.revoked_at[jti] >= self.rejects_after else 200
        return 200


class FakeServiceNow:
    url = "https://servicenow.example"

    def __init__(self):
        self.calls = []

    def find(self, table, query, fields=None):
        return {"sys_id": f"{table}-id"}

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if path == "/api/now/table/sys_choice":
            return [{"value": "Duplicate", "label": "Duplicate"}, {"value": "Solution provided", "label": "Solution provided"}]
        if method == "POST":
            return {"sys_id": "incident-id", "number": "INC0010041"}
        return {}


class FakeHost:
    def __init__(self, clock, jfrog, messages, current):
        self.clock, self.jfrog, self.messages = clock, jfrog, messages
        self.ssm = {"jcr/token": current, "sonar/token": "sonar-old"}
        self.sn = FakeServiceNow()
        self.replies, self.events, self.annotations, self.refreshed = [], [], [], []

    def get(self, name):
        return self.ssm[name]

    def put(self, name, value):
        self.ssm[name] = value

    def slack_history(self):
        return self.messages

    def reply(self, ts, text):
        self.replies.append((ts, text))

    def everyone(self):
        return {"sam": {"display_name": "Sam Developer", "slack_user_id": "U0SAM000001"}}

    def refresh_jenkins(self):
        self.clock.now += 5
        self.refreshed.append(self.ssm["jcr/token"])

    def event(self, body):
        self.events.append(body)

    def annotate(self, text):
        self.annotations.append(text)


PIPELINE_JTI = "3c4d5e6f-0000-4000-8000-000000000001"
TAKE_JTI = "7f3a2c1e-0000-4000-8000-000000000003"
DEAD_JTI = "11111111-0000-4000-8000-000000000004"


@pytest.fixture
def take(monkeypatch):
    """Sam pastes the take's token at 14:02:09; rotate.py starts at 14:02:16."""
    monkeypatch.setattr(inventory, "used_by", lambda name, files=None: ["scripts/deploy.py", "scripts/publish.py",
                                                                       "scripts/reset.py"])
    clock = Clock(at(14, 2, 16))
    pipeline, leaked = fake_token(PIPELINE_JTI), fake_token(TAKE_JTI)
    messages = [  # newest first, as conversations.history gives them
        {"ts": f"{at(14, 2, 9):.6f}", "user": "U0SAM000001", "text": f"Is this the right token for the registry? {leaked}"},
        {"ts": f"{at(11, 0, 0):.6f}", "user": "U0SAM000001", "text": f"An earlier take: {fake_token(DEAD_JTI)}"},
    ]
    jfrog = FakeJFrog(clock, {PIPELINE_JTI: pipeline, TAKE_JTI: leaked}, rejects_after=1.2)
    host = FakeHost(clock, jfrog, messages, pipeline)
    out = io.StringIO()
    steps = Steps(out=out, now=clock, sleep=clock.sleep)
    return host, steps, out, leaked, pipeline


def test_the_jfrog_rotation_prints_the_lines_the_video_reads(take):
    host, steps, out, leaked, pipeline = take
    assert jcr.run(steps, "pasted in Slack", host=host) == 0
    lines = out.getvalue().splitlines()
    assert lines == [
        "14:02:16  found     in #releases, posted 14:02:09",
        "14:02:16  revoked   old token 7f3a2c1e",
        "14:02:16  revoked   1 more of release-pipeline",
        "14:02:16  incident  INC0010041 opened",
        "14:02:16  issued    new token 2c91d0b4",
        "14:02:16  stored    SSM /release-demo/jcr/token",
        "14:02:21  reloaded  Jenkins credential jcr-token",
        "14:02:21  old token HTTP 401 from JFrog",
        "14:02:21  new token HTTP 200 from JFrog",
        "14:02:21  resolved  INC0010041",
        "Leak to useless: 0m 12s",
    ]
    assert all(len(line) <= 52 for line in lines)


def test_it_revokes_every_token_of_the_user_before_it_issues_one_leaked_first(take):
    host, steps, _, leaked, pipeline = take
    jcr.run(steps, "pasted in Slack", host=host)
    order = [call[:2] for call in host.jfrog.calls]
    assert order == [("revoke", TAKE_JTI), ("revoke", PIPELINE_JTI), ("issue",)]
    assert host.ssm["jcr/token"] == host.jfrog.issued
    assert host.refreshed == [host.jfrog.issued], "Jenkins reloads after SSM holds the new token"


def test_the_incident_holds_the_timeline_and_the_leak_time(take):
    host, steps, _, leaked, _ = take
    jcr.run(steps, "pasted in Slack", host=host)
    created = next(body for method, path, body in host.sn.calls if method == "POST")
    assert created["short_description"] == "JFrog token of release-pipeline exposed: pasted in Slack"
    assert (created["category"], created["impact"], created["urgency"]) == ("software", "2", "1")
    assert created["cmdb_ci"] == "cmdb_ci_appl-id" and created["caller_id"] == "sys_user-id"
    note = next(body["work_notes"] for method, path, body in host.sn.calls if body and "work_notes" in body)
    assert note.splitlines()[:6] == [
        "14:02:09  Token posted in #releases by Sam Developer",
        "14:02:16  Old token revoked in JFrog (id 7f3a2c1e), and 1 more of release-pipeline",
        "14:02:16  New token issued (id 2c91d0b4)",
        "14:02:21  New token in SSM; Jenkins reloaded",
        "14:02:21  Old token rejected by JFrog: HTTP 401",
        "14:02:21  New token accepted: HTTP 200",
    ]
    assert "The Slack message stays; the token in it no longer works." in note
    assert "scripts/deploy.py, scripts/publish.py and 1 more" in note
    resolved = next(body for method, path, body in host.sn.calls if body and body.get("state") == "6")
    assert resolved["close_code"] == "Solution provided"
    assert resolved["close_notes"] == ("Leak to useless: 0m 12s. Revoked, replaced and proven dead by "
                                       "scripts/rotate.py.")


def test_slack_gets_a_reply_in_the_leak_s_thread(take):
    host, steps, _, _, _ = take
    jcr.run(steps, "pasted in Slack", host=host)
    (ts, text), = host.replies
    assert ts == host.messages[0]["ts"]
    assert text == ("Revoked at 14:02:16 and replaced. JFrog rejects the old token (HTTP 401) since 14:02:21. "
                    "Incident INC0010041.")
    assert host.annotations and host.events[-1]["result"] == "ok" and host.events[-1]["leak_to_useless_seconds"] == 12


def test_no_token_value_reaches_any_record(take):
    host, steps, out, leaked, pipeline = take
    jcr.run(steps, "pasted in Slack", host=host)
    records_text = json.dumps([out.getvalue(), host.sn.calls, host.replies, host.events, host.annotations])
    for token in (leaked, pipeline, host.jfrog.issued):
        assert token not in records_text
        for part in token.split("."):
            assert part not in records_text


def test_leak_to_useless_is_the_first_401_minus_the_message_rounded_down():
    assert rotators.leak_to_useless(15.99) == "0m 15s"
    assert rotators.leak_to_useless(72.2) == "1m 12s"
    assert rotators.leak_to_useless(-1) == "0m 00s"


def test_with_no_message_the_leak_time_is_the_start_and_the_incident_says_so(take):
    host, steps, out, _, pipeline = take
    host.messages = [{"ts": "1.0", "user": "U0SAM000001", "text": "no token here"}]
    jcr.run(steps, "found in a log", host=host)
    assert out.getvalue().splitlines()[0] == "14:02:16  found     in no message: from now"
    created = next(body for method, path, body in host.sn.calls if method == "POST")
    assert "found in no message in #releases" in created["description"]
    assert host.replies == [] and host.jfrog.calls[0][:2] == ("revoke", PIPELINE_JTI)
    assert out.getvalue().splitlines()[-1] == "Leak to useless: 0m 05s"


def test_earlier_takes_dead_tokens_are_skipped(take):
    host, steps, out, _, _ = take
    host.messages = host.messages[1:]  # only the earlier take's message, whose token JFrog no longer lists
    jcr.run(steps, "pasted in Slack", host=host)
    assert out.getvalue().splitlines()[0] == "14:02:16  found     in no message: from now"


def test_a_token_jfrog_keeps_accepting_fails_and_leaves_the_incident_open(take):
    host, steps, out, _, _ = take
    host.jfrog.rejects_after = 10_000
    with pytest.raises(Failed, match="still accepts the old token"):
        jcr.run(steps, "pasted in Slack", host=host)
    assert "old token HTTP 200 after 120 s: FAIL" in out.getvalue()
    assert not [body for _, _, body in host.sn.calls if body and body.get("state") == "6"]
    note = next(body["work_notes"] for _, _, body in host.sn.calls if body and "work_notes" in body)
    assert "The incident stays open." in note and host.events[-1]["result"] == "failed"


def test_without_the_revoke_endpoint_the_token_revokes_itself(take):
    host, steps, _, _, _ = take
    host.jfrog.revoke_status = 404
    jcr.run(steps, "pasted in Slack", host=host)
    assert ("delete-me", TAKE_JTI) in host.jfrog.calls and ("delete-me", PIPELINE_JTI) in host.jfrog.calls


def test_rotate_prints_fail_and_exits_1(monkeypatch, capsys):
    class Broken:
        @staticmethod
        def run(steps, reason, **_):
            raise Failed("JFrog didn't list its tokens (502)")

    monkeypatch.setattr(rotate, "rotator", lambda name: Broken)
    assert rotate.rotate("jcr", "test", steps=Steps(out=None)) == 1
    assert capsys.readouterr().out.strip() == "FAIL: JFrog didn't list its tokens (502)"


class FakeSonar:
    def __init__(self, names):
        self.names_list, self.revoked, self.dead = list(names), [], set()

    def generate(self, token, name):
        self.names_list.append(name)
        return "sonar-new"

    def names(self, token):
        return self.names_list

    def revoke(self, token, name):
        self.revoked.append(name)
        self.dead.add("sonar-old")

    def valid(self, token):
        return token not in self.dead


def test_the_sonar_rotation_revokes_the_old_names_and_proves_the_old_token_dead(take, monkeypatch):
    host, steps, out, _, _ = take
    monkeypatch.setattr(sonar, "token_name", lambda: "release-demo-20261102T130216Z")
    fake = FakeSonar(["made by hand", "release-demo-20261001T090000Z", "Mateusz's laptop"])
    assert sonar.run(steps, "yearly", old_name="made by hand", host=host, sonar=fake) == 0
    assert fake.revoked == ["made by hand", "release-demo-20261001T090000Z"]
    assert host.ssm["sonar/token"] == "sonar-new"
    lines = out.getvalue().splitlines()
    assert lines[0] == "14:02:16  issued    release-demo-20261102T130216Z"
    assert "old token valid: false at SonarQube" in lines[-3] and all(len(l) <= 52 for l in lines[:-1])


def test_the_sonar_rotation_needs_the_hand_made_name_the_first_time(take, monkeypatch):
    host, steps, _, _, _ = take
    monkeypatch.setattr(sonar, "token_name", lambda: "release-demo-20261102T130216Z")
    with pytest.raises(Failed, match="--old-name"):
        sonar.run(steps, "yearly", host=host, sonar=FakeSonar(["made by hand"]))


# The secrets inventory

def test_every_secure_parameter_says_how_it_rotates():
    assert inventory.unclassified() == [], "add each one to infra/secret-rotation.yaml"
    assert set(inventory.rotations()) <= set(inventory.catalog()), "a rotation entry names no parameter"
    for name, entry in inventory.rotations().items():
        rotation = entry.get("rotation")
        assert entry.get("credential") is False or rotation["by"] in ("script", "hand"), name
        if rotation and rotation["by"] == "script":
            assert rotation["rotator"] in rotators.NAMES, name
        if rotation and rotation["by"] == "hand":
            assert rotation["how"], name


def test_the_counts_add_up_and_by_script_rows_come_first():
    rows = inventory.rows(metadata=None)
    total, script, hand = inventory.summary(rows)
    assert total == script + hand == len(rows)
    assert [r["name"] for r in rows[:script]] == ["jcr/token", "sonar/token"]


def test_unset_credentials_are_listed_apart():
    stamp = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.timezone.utc)
    metadata = {name: {"version": 3, "modified": stamp} for name in inventory.secure_names()}
    metadata["jamf/client-secret"]["version"] = 1  # "unset", as Terraform wrote it
    rows = inventory.rows(metadata)
    total, _, _ = inventory.summary(rows)
    assert total == len(rows) - 1
    page = inventory.render(rows, generated=stamp)
    assert "Not counted: 1 more credentials" in page and "jamf/client-secret" in page.split("Not counted")[1]


def test_the_page_names_where_each_secret_lives_and_who_reads_it():
    rows = {r["name"]: r for r in inventory.rows(metadata=None)}
    assert rows["jcr/token"]["lives_in"] == ["SSM", "tools host tmpfs", "Jenkins (credential jcr-token)"]
    assert "scripts/publish.py" in rows["jcr/token"]["used_by"]
    assert rows["cloudflare/api-token"]["lives_in"] == ["SSM"], "host_secrets.py leaves it off the host"
    assert rows["slack/bot-token"]["rotates"].startswith("By hand: api.slack.com/apps")
    assert rows["jira/api-token"]["rotates"].startswith("By hand: signed in as Release Pipeline")
    page = inventory.render(list(rows.values()))
    assert "<title>Secrets inventory</title>" in page
    total, script, hand = inventory.summary(list(rows.values()))
    assert f"<b>{total}</b> stored secrets · <b>{script}</b> rotate by script · <b>{hand}</b> rotate by hand" in page


def test_used_by_matches_whole_names_only(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    (tmp_path / "a.py").write_text('get_parameter(ssm, "jcr/token")\n')
    (tmp_path / "b.py").write_text('get_parameter(ssm, "jcr/token-old")\n"jcr/inventory-read-token"\n')
    (tmp_path / "c.yaml").write_text("secret: ${jcr-token:-not-set}\n")
    assert inventory.used_by("jcr/token", ["a.py", "b.py", "c.yaml"]) == ["a.py", "c.yaml"]
    assert inventory.used_by_text(["a", "b", "c", "d"]) == "a, b and 2 more"


# From the Mac

class FakeProcess:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_the_mac_prints_the_host_s_lines_and_nothing_else(capsys):
    lines = ["Starting session with SessionId: mateusz-0123\r\n", "== 2026-11-02T13:02:15Z rotate.py jcr\n",
             "14:02:16  found     in #releases, posted 14:02:09\n", "\n", "Leak to useless: 0m 12s\n", "== exit 0\n",
             "Exiting session with sessionId: mateusz-0123.\n"]
    process = FakeProcess(lines)
    seen = {}

    def popen(args, **kwargs):
        seen["args"] = args
        return process

    class TheHost:
        id = "i-0123"

    assert rotate.stream(TheHost(), 1200, popen=popen) == 0
    assert capsys.readouterr().out == "14:02:16  found     in #releases, posted 14:02:09\nLeak to useless: 0m 12s\n"
    assert process.terminated and "AWS-StartNonInteractiveCommand" in seen["args"]
    assert json.loads(seen["args"][-1]) == {"command": ["tail -c +1201 -F /var/log/release-demo-rotate.log"]}


def test_the_host_runs_it_as_its_own_unit_with_the_reason_quoted():
    class TheHost:
        id = "i-0123"
        scripts = []

        def shell(self, script, timeout=60):
            self.scripts.append(script)
            return 0, "started 5120\n", ""

    the_host = TheHost()
    assert rotate.start_on_host(the_host, ["jcr", "--reason", "pasted in Slack; rm -rf /"]) == ("started", 5120)
    script = the_host.scripts[0]
    assert "systemd-run --quiet --unit release-demo-rotate" in script
    assert "append:/var/log/release-demo-rotate.log" in script
    assert "--reason '\"'\"'pasted in Slack; rm -rf /'\"'\"'" in script, "the reason stays one quoted argument"


# The take's scripts

def leaked_reset():
    """scripts/demos/leaked-token/reset.py, which shares its name with scripts/reset.py."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("leaked_token_reset",
                                                  inventory.ROOT / "scripts" / "demos" / "leaked-token" / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clipboard_shows_four_characters_and_clears_only_its_own_token():
    import clipboard

    calls, out = [], io.StringIO()
    token = fake_token(TAKE_JTI)

    def run(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        return subprocess.CompletedProcess(args, 0, token if args == ["pbpaste"] else "", "")

    clipboard.copy(token, run=run, sleep=lambda s: None, out=out)
    assert calls[0] == (["pbcopy"], token) and calls[-1] == (["pbcopy"], "")
    assert token[-4:] in out.getvalue() and token[:-4] not in out.getvalue()

    calls.clear()
    clipboard.copy(token, run=lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "other", ""),
                   sleep=lambda s: None, out=out)
    assert calls == [["pbcopy"], ["pbpaste"]], "something else on the clipboard is left alone"


def test_the_reset_revokes_a_stray_take_token_and_keeps_the_pipeline_s(monkeypatch):
    import take as take_module

    reset = leaked_reset()
    pipeline = fake_token(PIPELINE_JTI)
    revoked = []
    monkeypatch.setattr("jcr_setup.token_works", lambda client, token: token == pipeline)
    monkeypatch.setattr("common.get_parameter", lambda ssm, name: pipeline)
    monkeypatch.setattr(take_module, "user_tokens", lambda client: [{"token_id": PIPELINE_JTI}, {"token_id": TAKE_JTI}])
    monkeypatch.setattr(take_module, "revoke", lambda client, token_id: revoked.append(token_id) or 200)

    class Act:
        dry_run, actions = False, 0

        def act(self, what, do):
            self.actions += 1
            do()

    reset.step_jfrog(Act(), client=object(), ssm=object())
    assert revoked == [TAKE_JTI]


def test_the_reset_resolves_a_leak_incident_a_take_left_open():
    reset = leaked_reset()

    class SN(FakeServiceNow):
        def call(self, method, path, body=None, **query):
            if path == "/api/now/table/incident" and method == "GET":
                self.calls.append((method, path, query))
                return [{"sys_id": "i1", "number": "INC0010041", "short_description": records.INCIDENT_PREFIX + "x"}]
            return super().call(method, path, body, **query)

    sn = SN()

    class Act:
        def act(self, what, do):
            do()

    reset.step_incidents(Act(), sn=sn)
    patch = next(body for method, path, body in sn.calls if method == "PATCH")
    assert patch["close_notes"] == "Resolved by reset.py before the next take." and patch["state"] == "6"


def test_the_take_token_is_described_by_id_and_age_only():
    import take as take_module

    token = fake_token(PIPELINE_JTI, iat=1_793_000_000)
    assert take_module.describe(token, now=1_793_000_000 + 3 * 86400) == "id 3c4d5e6f, 3 days old"


def test_the_slack_app_asks_for_the_history_scopes():
    import yaml

    manifest = yaml.safe_load((inventory.ROOT / "slack" / "manifest.yml").read_text())
    assert {"channels:history", "groups:history"} <= set(manifest["oauth_config"]["scopes"]["bot"])


def test_servicenow_setup_adds_the_registry_item_and_amsterdam_time(monkeypatch):
    import servicenow_setup

    ensured = []

    class SN:
        def ensure(self, table, query, values, label):
            ensured.append((table, values))
            return {"sys_id": f"{label}-id"}

        def call(self, *args, **kwargs):
            return {}

        def find(self, table, query, fields=None):
            return {"sys_id": "role-id", "user_password": "set"}

    monkeypatch.setattr(servicenow_setup, "services", lambda: {})
    servicenow_setup.setup_cmdb(SN(), {"Platform team": "group-id"})
    assert ensured == [("cmdb_ci_appl", {"name": "jfrog-container-registry",
                                        "short_description": servicenow_setup.PLATFORM_ITEMS["jfrog-container-registry"],
                                        "support_group": "group-id", "operational_status": "1"})]
    ensured.clear()
    monkeypatch.setattr(servicenow_setup, "get_parameter_or_none", lambda ssm, name: "Password-1!")
    servicenow_setup.setup_people(SN(), None)
    zones = {values["user_name"]: values.get("time_zone") for table, values in ensured if table == "sys_user"}
    assert zones["sam.developer"] == zones["alex.change.manager"] == zones["release.pipeline"] == "Europe/Amsterdam"


def test_the_check_reads_hashes_never_values():
    check = (inventory.ROOT / "scripts" / "demos" / "leaked-token" / "check.py").read_text()
    assert "MessageDigest" in check and "println c.secret" not in check
    assert "print(current" not in check and "print(old" not in check


def test_the_reset_loads_the_shared_check_module_not_its_folder_s():
    """Run as a script, the reset has its own folder first on its path, where check.py is
    the acceptance checks; the main reset imports scripts/check.py as check."""
    import sys

    folder = inventory.ROOT / "scripts" / "demos" / "leaked-token"
    code = (f"import sys; sys.path.insert(0, {str(folder)!r}); import reset; shared = reset.main_reset(); "
            "import check; print(shared.wait_for_version.__module__, check.__file__)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=inventory.ROOT)
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.split() == ["check", str(inventory.ROOT / "scripts" / "check.py")]


def test_the_check_asks_jenkins_for_a_crumb_before_its_script_console():
    """Without a crumb Jenkins answers the POST with 403, and check 3 read that as 'Jenkins differs'."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("leaked_token_check",
                                                  inventory.ROOT / "scripts" / "demos" / "leaked-token" / "check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)

    class Jenkins:
        calls = []

        def call(self, method, path, body=None, headers=None):
            self.calls.append((method, path, dict(body or {}), dict(headers or {})))
            if path == "/crumbIssuer/api/json":
                return 200, {"crumbRequestField": "Jenkins-Crumb", "crumb": "c0ffee"}
            if not (headers or {}).get("Jenkins-Crumb"):
                return 403, "No valid crumb was included in the request"
            return 200, "956bcd79d3ef\n"

    jenkins = Jenkins()
    assert check.jenkins_hash(None, client=jenkins) == "956bcd79d3ef"
    method, path, body, headers = jenkins.calls[-1]
    assert (method, path, headers) == ("POST", "/scriptText", {"Jenkins-Crumb": "c0ffee"})
    assert "MessageDigest" in body["script"]
