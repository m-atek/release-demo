"""Demo 7's scripts: the .ipa and .aab readers, the stand-ins, the package, and
a release's deploy and check against the stand-ins, with JCR and SSM faked.

    .venv/bin/python -m pytest -q scripts/demos/mobile/tests
"""

import io
import json
import plistlib
import sys
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
# Appended: the shared tests import scripts/check.py and friends by the same names.
sys.path.append(str(HERE))

import google_play  # noqa: E402
import jamf_apps  # noqa: E402
import mobile  # noqa: E402
import run_state  # noqa: E402
import testflight  # noqa: E402
from standin import ServiceError  # noqa: E402

build, check, deploy, reset, flutter_test = (mobile.local(name) for name in ("build", "check", "deploy", "reset", "test"))

BUNDLE = "nl.es.demo.storefloor"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_STANDIN_DIR", str(tmp_path / "standins"))
    monkeypatch.setenv("STANDIN", "1")
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    monkeypatch.setattr(build, "DIST", tmp_path / "dist" / "store-floor")
    monkeypatch.setattr(build, "PARTS", tmp_path / "dist" / "store-floor" / "parts")
    monkeypatch.setattr(deploy, "note", lambda text: None)
    monkeypatch.setattr(deploy, "release_notes", lambda state: "Store Floor 2.4.0\n- Show stock alerts (ORD-80)")
    reset.initial_standins(mobile.state_store(), build="52")
    return tmp_path


# --- Building blocks ---------------------------------------------------------


def runner_app(tmp_path, version="2.4.0", number="57", encryption=True):
    app = tmp_path / f"app-{version}-{number}" / "Runner.app"
    (app / "Frameworks" / "App.framework").mkdir(parents=True, exist_ok=True)
    (app / "Frameworks" / "App.framework" / "App").write_bytes(b"compiled dart")
    (app / "Runner").write_bytes(b"\xcf\xfa\xed\xfe binary")
    info = {"CFBundleIdentifier": BUNDLE, "CFBundleShortVersionString": version, "CFBundleVersion": number,
            "CFBundleDisplayName": "Store Floor"}
    if encryption:
        info["ITSAppUsesNonExemptEncryption"] = False
    (app / "Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
    return app


def ipa(tmp_path, method="ad-hoc", **kwargs):
    target = tmp_path / f"{method}-{kwargs.get('number', '57')}.ipa"
    build.standin_export(runner_app(tmp_path, **kwargs), method, target)
    return target.read_bytes()


def varint(value):
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def attribute(name, value):
    return field(4, field(2, name.encode()) + field(3, value.encode()))


def element(name, attributes, children=()):
    body = field(3, name.encode()) + b"".join(attribute(k, v) for k, v in attributes.items())
    body += b"".join(field(5, child) for child in children)
    return field(1, body)


def aab(version_code=57, version_name="2.4.0", target_sdk=36, package=BUNDLE, certificate=None):
    """An app bundle with aapt2's protobuf manifest. The signature check is
    replaced by `certificate`, the fingerprint the fake signer reports."""
    manifest = element("manifest", {"package": package, "versionCode": str(version_code), "versionName": version_name},
                       [element("uses-sdk", {"minSdkVersion": "24", "targetSdkVersion": str(target_sdk)})])
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("base/manifest/AndroidManifest.xml", manifest)
        archive.writestr("base/dex/classes.dex", b"dex")
        if certificate:
            archive.writestr("META-INF/UPLOAD.RSA", certificate.encode())
    return data.getvalue()


@pytest.fixture(autouse=True)
def fake_signer(monkeypatch):
    monkeypatch.setattr(mobile, "signer_sha256", lambda block: block.decode())


def test_standin_setting(monkeypatch):
    monkeypatch.delenv("STANDIN")
    assert mobile.standins({"STANDIN": "1"}) == {"apple", "google-play", "jamf"}
    assert mobile.standins({"STANDIN": "jamf, apple"}) == {"jamf", "apple"}


def test_standin_setting_none_and_unknown(monkeypatch):
    monkeypatch.delenv("STANDIN")
    assert mobile.standins({"STANDIN": "0"}) == set()
    with pytest.raises(SystemExit):
        mobile.standins({"STANDIN": "jamf,windows"})


def test_ipa_info_reads_the_export(tmp_path):
    info = mobile.ipa_info(ipa(tmp_path, "app-store-connect"))
    assert (info["bundle_id"], info["version"], info["build"]) == (BUNDLE, "2.4.0", "57")
    assert info["method"] == "app-store-connect" and info["signed"] and info["encryption_declared"]


def test_aab_info_reads_the_protobuf_manifest():
    info = mobile.aab_info(aab(certificate="AA:BB"))
    assert (info["package"], info["version_code"], info["version_name"], info["target_sdk"]) == (BUNDLE, 57, "2.4.0", 36)
    assert info["signed"] and info["upload_certificate_sha256"] == "AA:BB"
    assert not mobile.aab_info(aab())["signed"]


def test_rebuild_numbers_go_above_every_build():
    assert deploy.next_rebuild_number("57") == "57.1"
    assert deploy.next_rebuild_number("57", "57.1", None) == "57.2"
    assert jamf_apps.build_key("57.10") > jamf_apps.build_key("57.9") > jamf_apps.build_key("57")


# --- Stand-ins ----------------------------------------------------------------


def test_jamf_upload_reaches_the_pilot_only(tmp_path):
    jamf = jamf_apps.jamf()
    app = jamf.find_app("Store Floor")
    assert (app["version"], app["scope"]) == ("2.3.0", ["Pilot"])
    jamf.upload_ipa(app["id"], "x.ipa", ipa(tmp_path))
    assert jamf.installed(1, BUNDLE)["version"] == "2.4.0"
    assert jamf.installed(5, BUNDLE)["version"] == "2.3.0"
    jamf.set_scope(app["id"], ["All store devices"])
    assert all(jamf.installed(d["id"], BUNDLE)["version"] == "2.4.0" for d in jamf.devices("All store devices"))
    requests = [r["request"] for r in mobile.state_store().read("jamf.json")["requests"]]
    assert "POST /JSSResource/fileuploads/mobiledeviceapplicationsipa/id/7" in requests


def test_jamf_refuses_an_app_store_build_and_ios_keeps_a_newer_build(tmp_path):
    jamf = jamf_apps.jamf()
    with pytest.raises(ServiceError, match="ad hoc"):
        jamf.upload_ipa(7, "x.ipa", ipa(tmp_path, "app-store-connect"))
    jamf.upload_ipa(7, "old.ipa", ipa(tmp_path, version="2.3.0", number="40"))
    device = mobile.state_store().read("jamf.json")["devices"]["1"]
    assert device["apps"][BUNDLE]["build"] == "52" and device["last_result"].startswith("Failed")


def test_testflight_processes_and_refuses_a_second_upload(tmp_path):
    flight = testflight.testflight()
    with pytest.raises(ServiceError, match="ITMS-90161"):
        flight.upload("x.ipa", ipa(tmp_path, "ad-hoc"), "2.4.0", "57")
    uploaded = flight.upload("x.ipa", ipa(tmp_path, "app-store-connect"), "2.4.0", "57")
    found = flight.build("2.4.0", "57")
    assert found["processing"] == "VALID" and found["internal_state"] == "READY_FOR_BETA_TESTING"
    with pytest.raises(ServiceError, match="ITMS-4238"):
        flight.upload("x.ipa", ipa(tmp_path, "app-store-connect"), "2.4.0", "57")
    flight.release_to_group(uploaded["id"], "Store team", "Notes")
    assert flight.build("2.4.0", "57")["internal_state"] == "IN_BETA_TESTING"


def test_testflight_waits_for_export_compliance(tmp_path):
    flight = testflight.testflight()
    uploaded = flight.upload("x.ipa", ipa(tmp_path, "app-store-connect", encryption=False), "2.4.0", "57")
    assert flight.build("2.4.0", "57")["internal_state"] == "MISSING_EXPORT_COMPLIANCE"
    with pytest.raises(ServiceError, match="export compliance"):
        flight.release_to_group(uploaded["id"], "Store team", "Notes")


def test_google_play_checks_key_version_code_and_target_sdk():
    play = google_play.google_play()
    with pytest.raises(ServiceError, match="not signed"):
        play.upload_and_release("internal", "x.aab", aab(), "draft", "n", "2.4.0 (57)")
    assert play.upload_and_release("internal", "x.aab", aab(certificate="AA"), "draft", "n", "2.4.0 (57)") == 57
    with pytest.raises(ServiceError, match="already been used"):
        play.upload_and_release("internal", "x.aab", aab(certificate="AA"), "draft", "n", "2.4.0 (57)")
    with pytest.raises(ServiceError, match="wrong key"):
        play.upload_and_release("internal", "x.aab", aab(58, certificate="BB"), "draft", "n", "2.4.0 (58)")
    with pytest.raises(ServiceError, match="API level 36"):
        play.upload_and_release("internal", "x.aab", aab(59, target_sdk=35, certificate="AA"), "draft", "n", "x")
    play.set_release("internal", 57, "completed", "Notes", "2.4.0 (57)")
    assert play.track("internal")["releases"][0]["status"] == "completed"


# --- The package --------------------------------------------------------------


def parts(tmp_path, version="2.4.0", number="57"):
    names = build.names(version, number)
    (build.PARTS / "ios").mkdir(parents=True)
    (build.PARTS / "android").mkdir(parents=True)
    (build.PARTS / "ios" / names["adhoc"]).write_bytes(ipa(tmp_path, "ad-hoc", version=version, number=number))
    (build.PARTS / "ios" / names["appstore"]).write_bytes(ipa(tmp_path, "app-store-connect", version=version, number=number))
    (build.PARTS / "ios" / "ios-signing.json").write_text("{}")
    (build.PARTS / "android" / names["aab"]).write_bytes(aab(int(number), version, certificate="AA:BB"))
    (build.PARTS / "android" / "android-signing.json").write_text("{}")


def test_assemble_writes_the_package_and_its_checksums(tmp_path):
    parts(tmp_path)
    build.assemble("2.4.0", "57", "ORD-80", "abc1234", ["ios", "android"])
    info = json.loads((build.DIST / "build-info.json").read_text())
    assert info["store_builds"] == {"ios": "57", "android": 57}
    assert info["sha256"] == mobile.file_sha256(build.DIST / "store-floor-2.4.0.zip")
    with zipfile.ZipFile(build.DIST / "store-floor-2.4.0.zip") as archive:
        assert {"SHA256SUMS", "pubspec.lock", "ios/store-floor-2.4.0-57-adhoc.ipa",
                "android/store-floor-2.4.0-57.aab"} <= set(archive.namelist())


def test_assemble_refuses_a_wrong_build_number(tmp_path):
    parts(tmp_path, number="56")
    for path in list((build.PARTS).rglob("*-56*")):
        path.rename(str(path).replace("-56", "-57"))
    with pytest.raises(SystemExit):
        build.assemble("2.4.0", "57", "ORD-80", "abc1234", ["ios", "android"])


def test_junit_from_flutter_events():
    events = [{"type": "suite", "suite": {"id": 0, "path": "/w/services/store-floor/test/a_test.dart"}},
              {"type": "testStart", "test": {"id": 1, "name": "loading", "suiteID": 0}, "time": 0},
              {"type": "testDone", "testID": 1, "result": "success", "hidden": True, "time": 5},
              {"type": "testStart", "test": {"id": 2, "name": "works", "suiteID": 0}, "time": 5},
              {"type": "testDone", "testID": 2, "result": "success", "hidden": False, "time": 25},
              {"type": "testStart", "test": {"id": 3, "name": "breaks", "suiteID": 0}, "time": 25},
              {"type": "error", "testID": 3, "error": "Expected: 1", "stackTrace": "at a_test.dart"},
              {"type": "testDone", "testID": 3, "result": "failure", "hidden": False, "time": 30}]
    report, failed = flutter_test.junit(events, root="/w/services/store-floor")
    assert failed == 1 and 'name="test/a_test.dart" tests="2"' in report and "Expected: 1" in report
    assert flutter_test.rebase_lcov("SF:lib/main.dart\nDA:1,1\n", "services/store-floor") == \
        "SF:services/store-floor/lib/main.dart\nDA:1,1\n"


# --- A release against the stand-ins ------------------------------------------


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A built and published 2.4.0 (57), with JCR and SSM faked."""
    parts(tmp_path)
    build.assemble("2.4.0", "57", "ORD-80", "abc1234", ["ios", "android"])
    package = (build.DIST / "store-floor-2.4.0.zip").read_bytes()
    info = json.loads((build.DIST / "build-info.json").read_text())
    monkeypatch.setattr(deploy, "get_parameter", lambda client, name: "token")
    monkeypatch.setattr(deploy, "ssm_client", lambda: None)
    monkeypatch.setattr(deploy, "download", lambda token, path: package)

    def start(option):
        run_state.save({"service": "store-floor", "version": "2.4.0", "jira_key": "ORD-80", "option": option,
                        "build": {"number": "57"},
                        "package": {"path": "demos-candidates/store-floor/2.4.0/57/store-floor-2.4.0.zip",
                                    "sha256": info["sha256"]}})
    return start


def run(script, *args, monkeypatch=None):
    monkeypatch.setattr(sys, "argv", [script.__name__, "store-floor", *args])
    try:
        script.main()
        return 0
    except SystemExit as stop:
        return stop.code or 0


def test_jamf_release_pilot_then_every_store(release, monkeypatch, capsys):
    release("jamf")
    assert run(deploy, "staging", monkeypatch=monkeypatch) == 0
    assert run(check, "staging", "--version", "2.4.0", monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "ok   Jamf: scoped to Pilot" in out and "ok   Utrecht Centraal iPad reports Store Floor 2.4.0 (57)" in out
    assert run_state.load()["deploys"]["staging"]["previous_version"] == "2.3.0"
    assert run(deploy, "production", monkeypatch=monkeypatch) == 0
    assert run(check, "production", "--version", "2.4.0", monkeypatch=monkeypatch) == 0
    assert "ok   12 of 12 store devices report 2.4.0" in capsys.readouterr().out


def test_jamf_rollback_rebuilds_with_a_higher_build(release, monkeypatch, tmp_path, capsys):
    release("jamf")
    run(deploy, "staging", monkeypatch=monkeypatch)
    run(deploy, "production", monkeypatch=monkeypatch)
    rebuilt = []
    monkeypatch.setattr(deploy, "rebuild", lambda version, number: rebuilt.append((version, number)) or (
        "r.ipa", ipa(tmp_path, version=version, number=number)))
    assert run(deploy, "production", "--back", monkeypatch=monkeypatch) == 0
    assert rebuilt == [("2.3.0", "57.1")]
    assert run(check, "production", "--version", "2.3.0", monkeypatch=monkeypatch) == 0
    assert "reports Store Floor 2.3.0 (57.1)" in capsys.readouterr().out


def test_stores_release_to_both_testing_tracks(release, monkeypatch, capsys):
    release("stores")
    assert run(deploy, "staging", monkeypatch=monkeypatch) == 0
    assert run(check, "staging", "--version", "2.4.0", monkeypatch=monkeypatch) == 0
    assert "ok   Google Play: version code 57 on the internal track: draft" in capsys.readouterr().out
    assert run_state.load()["change_details"] == ["TestFlight build: 2.4.0 (57)",
                                                  "Google Play version code: 57 (internal track)"]
    assert run(deploy, "production", monkeypatch=monkeypatch) == 0
    assert run(check, "production", "--version", "2.4.0", monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "in beta testing for the internal group Store team" in out
    assert "ok   Google Play: release notes: Store Floor 2.4.0" in out
    assert run(deploy, "production", "--back", monkeypatch=monkeypatch) == 0
    assert run(check, "production", "--version", "2.3.0", monkeypatch=monkeypatch) == 0


def test_a_changed_package_is_not_deployed(release, monkeypatch):
    release("jamf")
    monkeypatch.setattr(deploy, "download", lambda token, path: b"tampered")
    assert run(deploy, "staging", monkeypatch=monkeypatch) == 1


def test_a_release_needs_an_option(release, monkeypatch):
    release("")
    assert run(deploy, "staging", monkeypatch=monkeypatch) == 1


def test_reset_standins_twice(release, monkeypatch):
    release("jamf")
    run(deploy, "staging", monkeypatch=monkeypatch)
    run(deploy, "production", monkeypatch=monkeypatch)
    store = mobile.state_store()
    for _ in range(2):
        reset.reset_standins(store)
        state = store.read("jamf.json")
        app = state["apps"]["7"]
        assert (app["version"], app["build"], app["scope"]) == ("2.3.0", "57.1", ["Pilot"])
        assert {d["apps"][BUNDLE]["version"] for d in state["devices"].values()} == {"2.3.0"}


def test_build_job_runs_and_its_files_come_back(tmp_path, monkeypatch):
    """jenkins.build against a fake Jenkins: the Mac is online, the job queues,
    runs, succeeds, and its archived files download into dist/store-floor/parts."""
    import http.server
    import threading

    import jenkins

    started = {}

    class FakeJenkins(http.server.BaseHTTPRequestHandler):
        def reply(self, body, status=200, headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            started["parameters"] = self.rfile.read(length).decode()
            started["auth"] = self.headers["Authorization"]
            self.reply(b"", 201, [("Location", f"http://127.0.0.1:{port}/queue/item/7/")])

        def do_GET(self):
            if self.path.startswith("/label/mac/"):
                self.reply({"nodes": [{"nodeName": "mac", "offline": False}]})
            elif self.path.startswith("/queue/item/7/"):
                # Jenkins answers with its public address, which the client must not follow.
                self.reply({"executable": {"number": 12, "url": "https://jenkins-demo.es.nl/job/store-floor-build/12/"}})
            elif self.path.startswith("/job/store-floor-build/12/api/json"):
                self.reply({"building": False, "result": "SUCCESS",
                            "artifacts": [{"relativePath": "dist/store-floor/parts/ios/a.ipa"}]})
            elif self.path == "/job/store-floor-build/12/artifact/dist/store-floor/parts/ios/a.ipa":
                self.reply(b"ipa bytes")
            else:
                self.reply({}, 404)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeJenkins)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(mobile, "ROOT", tmp_path)
    try:
        client = jenkins.Jenkins(url=f"http://127.0.0.1:{port}", token="t0ken")
        target = jenkins.build("2.3.0", "57.1", "demo-state:2.3.0", ["ios"], "", jenkins=client, say=lambda text: None)
    finally:
        server.shutdown()
    assert (target / "a.ipa").read_bytes() == b"ipa bytes"
    assert "STORE_BUILD=57.1" in started["parameters"] and "SOURCE=demo-state%3A2.3.0" in started["parameters"]
