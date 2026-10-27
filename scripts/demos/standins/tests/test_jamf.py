"""The Jamf Pro stand-in and its client, over real HTTP on this machine.

    .venv/bin/python -m pytest -q scripts/demos/standins/tests
"""

import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import jamf  # noqa: E402

AGENT = HERE.parent / "jamf-agent.sh"
SECRETS = {"client_id": "client", "client_secret": "secret", "signing_key": "key", "enrollment_code": "invite"}


@pytest.fixture
def server(tmp_path):
    os.environ["JAMF_STANDIN_QUIET"] = "1"
    httpd = jamf.make_server(tmp_path / "state", 0, host="127.0.0.1", **SECRETS)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def client(server):
    return jamf.Jamf(server, "client", "secret", standin=True)


def setup_groups(client):
    client.create_smart_computer_group("Pilot", [("Computer Name", "like", "pilot")])
    client.create_smart_computer_group("All managed Macs", [])
    return client.create_policy("Store Assistant", ["Pilot"])


def fake_pkg(tmp_path, version):
    """A flat package of a tiny Store Assistant.app, made with pkgbuild."""
    if not shutil.which("pkgbuild"):
        pytest.skip("needs macOS pkgbuild")
    root = tmp_path / f"root-{version}"
    app = root / "Applications" / "Store Assistant.app" / "Contents"
    (app / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "MacOS" / "StoreAssistant").write_text("#!/bin/sh\n")
    with open(app / "Info.plist", "wb") as file:
        plistlib.dump({"CFBundleIdentifier": "nl.es.StoreAssistant", "CFBundleShortVersionString": version,
                       "CFBundleExecutable": "StoreAssistant", "CFBundlePackageType": "APPL"}, file)
    pkg = tmp_path / f"StoreAssistant-{version}.pkg"
    subprocess.run(["pkgbuild", "--root", str(root), "--identifier", "nl.es.StoreAssistant", "--version", version,
                    "--install-location", "/", str(pkg)], check=True, capture_output=True)
    return pkg


def test_a_wrong_secret_gets_no_token(server):
    with pytest.raises(jamf.JamfError, match="401"):
        jamf.Jamf(server, "client", "wrong").token()


def test_calls_without_a_token_are_refused(server):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{server}/JSSResource/policies/name/Store%20Assistant")
    assert error.value.code == 401


def test_policy_package_scope_and_flush(client, tmp_path):
    policy_id = setup_groups(client)
    record = client.upload_package(fake_pkg(tmp_path, "1.7.0"))
    assert record["packageName"] == "StoreAssistant-1.7.0.pkg"
    assert record["hashType"] == "SHA_512" and len(record["hashValue"]) == 128
    client.set_policy_package(policy_id, record["id"])
    policy = client.policy("Store Assistant")
    assert [p["name"] for p in policy["packages"]] == ["StoreAssistant-1.7.0.pkg"]
    assert policy["scope"] == {"all": False, "groups": ["Pilot"]}
    assert policy["trigger_checkin"] and policy["frequency"] == "Once per computer" and policy["recon"]
    client.set_policy_scope(policy_id, ["All managed Macs"])
    assert client.policy("Store Assistant")["scope"]["groups"] == ["All managed Macs"]
    client.flush_policy_logs(policy_id)
    # Uploading the same name again replaces the file in the same record.
    again = client.upload_package(fake_pkg(tmp_path, "1.7.0"))
    assert again["id"] == record["id"]


def test_reads_what_a_package_installs(tmp_path):
    contents = jamf.package_contents(fake_pkg(tmp_path, "1.6.0").read_bytes())
    assert contents == [{"name": "Store Assistant", "bundleId": "nl.es.StoreAssistant", "version": "1.6.0",
                         "path": "/Applications/Store Assistant.app"}]


def test_agentless_macs_install_what_is_scoped_to_them(client, tmp_path):
    policy_id = setup_groups(client)
    on = [{"name": "Store Assistant", "version": "1.6.0", "bundleId": "nl.es.StoreAssistant", "path": "/Applications/Store Assistant.app"}]
    client.add_simulated_device("computer", "pilot-sim-01", on)
    client.add_simulated_device("computer", "store-mac-02", on)
    record = client.upload_package(fake_pkg(tmp_path, "1.7.0"))
    client.set_policy_package(policy_id, record["id"])
    versions = {r["name"]: r["version"] for r in client.app_versions("All managed Macs", "nl.es.StoreAssistant")}
    assert versions == {"pilot-sim-01": "1.7.0", "store-mac-02": "1.6.0"}
    client.set_policy_scope(policy_id, ["All managed Macs"])
    assert {r["version"] for r in client.app_versions("All managed Macs", "nl.es.StoreAssistant")} == {"1.7.0"}
    # Reset puts an agentless Mac back on exactly what it's given.
    client.add_simulated_device("computer", "store-mac-02", on)
    assert {r["name"]: r["version"] for r in client.app_versions("All managed Macs", "nl.es.StoreAssistant")}["store-mac-02"] == "1.6.0"


def ipa(version, build):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("Payload/Runner.app/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": "nl.es.StoreFloor", "CFBundleShortVersionString": version, "CFBundleVersion": build,
             "CFBundleDisplayName": "Store Floor"}, fmt=plistlib.FMT_BINARY))
    return data.getvalue()


def test_an_in_house_app_widens_from_pilot_to_every_store(client, tmp_path):
    client.create_smart_mobile_group("Pilot", [("Display Name", "like", "pilot")])
    client.create_smart_mobile_group("All store devices", [])
    for name in ("pilot-ipad-01", "store-ipad-02"):
        client.add_simulated_device("mobile_device", name)
    app_id = client.create_mobile_app("Store Floor", "nl.es.StoreFloor", ["Pilot"])
    path = tmp_path / "StoreFloor.ipa"
    path.write_bytes(ipa("2.4.0", "31"))
    client.upload_ipa(app_id, path)
    app = client.mobile_app("Store Floor")
    assert app["version"] == "2.4.0" and app["scope"]["groups"] == ["Pilot"]
    assert {r["name"]: r["version"] for r in client.mobile_app_versions("All store devices", "nl.es.StoreFloor")} == \
        {"pilot-ipad-01": "2.4.0", "store-ipad-02": None}
    client.set_mobile_app_scope(app_id, ["All store devices"])
    assert {r["version"] for r in client.mobile_app_versions("All store devices", "nl.es.StoreFloor")} == {"2.4.0"}


def test_a_big_upload_goes_through_the_upload_store(client, tmp_path, monkeypatch):
    setup_groups(client)
    monkeypatch.setattr(jamf, "UPLOAD_LIMIT", 10)
    record = client.upload_package(fake_pkg(tmp_path, "1.7.1"))
    assert record["size"] > 10 and record["hashValue"]


def test_the_page_shows_scope_and_inventory_after_a_signed_login(client, server, tmp_path):
    policy_id = setup_groups(client)
    client.add_simulated_device("computer", "store-mac-02", [])
    client.set_policy_package(policy_id, client.upload_package(fake_pkg(tmp_path, "1.7.0"))["id"])
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{server}/standin/ui/state.json")
    assert error.value.code == 401
    token = jamf.Signer("key").sign("ui", 60)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as login:
        opener.open(f"{server}/standin/ui/login?token={token}")
    cookie = login.value.headers["Set-Cookie"].split(";")[0]
    request = urllib.request.Request(f"{server}/standin/ui/state.json", headers={"Cookie": cookie})
    view = json.loads(urllib.request.urlopen(request).read())
    policy = view["items"][0]
    assert policy["name"] == "Store Assistant" and policy["version"] == "1.7.0"
    assert policy["scope"] == ["Pilot"] and policy["packages"] == ["StoreAssistant-1.7.0.pkg"]
    assert [g["group"] for g in policy["inventory"]] == ["Pilot", "All managed Macs"]
    page = urllib.request.urlopen(urllib.request.Request(f"{server}/standin/ui", headers={"Cookie": cookie})).read().decode()
    assert "stand-in" not in page.lower() and "simulated" not in page.lower() and "jamf" not in page.lower()


@pytest.mark.skipif(sys.platform != "darwin", reason="the agent runs on macOS")
def test_the_agent_enrolls_installs_and_reports(client, server, tmp_path):
    policy_id = setup_groups(client)
    client.set_policy_package(policy_id, client.upload_package(fake_pkg(tmp_path, "1.7.0"))["id"])
    env = {**os.environ, "JAMF_AGENT_ROOT": str(tmp_path / "mac"), "JAMF_AGENT_NAME": "pilot-mac-01",
           "JAMF_AGENT_SERIAL": "TEST0001"}
    run = lambda *args: subprocess.run(["bash", str(AGENT), *args], env=env, capture_output=True, text=True, check=True).stdout
    assert "Enrolled pilot-mac-01" in run("enroll", "-url", server, "-code", "invite")
    output = run("policy")
    assert "Executing Policy Store Assistant" in output and "Successfully installed StoreAssistant-1.7.0.pkg." in output
    assert (tmp_path / "mac" / "Applications" / "Store Assistant.app").is_dir()
    assert client.app_versions("Pilot", "nl.es.StoreAssistant")[0]["version"] == "1.7.0"
    assert "No policies were found" in run("policy")
    client.flush_policy_logs(policy_id)
    assert "Executing Policy Store Assistant" in run("policy")
