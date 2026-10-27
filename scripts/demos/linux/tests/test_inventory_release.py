"""The helper Ansible runs on each server: checksum before unpacking, one folder
per release, tokens only to JCR, and Tomcat's error in plain words."""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from conftest import HERE, load

helper = load("inventory_release", HERE / "ansible" / "files" / "inventory-release")


def package(tmp_path, version="2.8.0"):
    war = io.BytesIO()
    with zipfile.ZipFile(war, "w") as archive:
        archive.writestr("WEB-INF/web.xml", "<web-app/>")
        archive.writestr("WEB-INF/classes/inventory-build.properties", f"version={version}\n")
    path = tmp_path / f"inventory-{version}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("inventory.war", war.getvalue())
        archive.writestr("VERSION", version + "\n")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "BASE", tmp_path / "opt")
    return tmp_path / "opt"


def install(path, sha, version="2.8.0"):
    helper.install(SimpleNamespace(version=version, sha256=sha, file=str(path), url=None, env_file=None, token_ssm=None))


def test_install_unpacks_one_folder_per_release(tmp_path, base, capsys):
    path, sha = package(tmp_path)
    install(path, sha)
    release = base / "releases" / "2.8.0"
    assert (release / "webapp" / "WEB-INF" / "web.xml").exists()
    assert (release / "package.sha256").read_text().strip() == sha
    assert not (release / "inventory.war").exists()
    assert "SHA-256 verified" in capsys.readouterr().out


def test_the_same_package_twice_changes_nothing(tmp_path, base, capsys):
    path, sha = package(tmp_path)
    install(path, sha)
    install(path, sha)
    assert "already holds this package" in capsys.readouterr().out


def test_a_changed_package_is_refused_before_unpacking(tmp_path, base, capsys):
    path, sha = package(tmp_path)
    with pytest.raises(SystemExit):
        install(path, "0" * 64)
    assert "Not deploying it" in capsys.readouterr().out
    assert not (base / "releases" / "2.8.0").exists()


def test_status_names_the_release_before_the_current_one(tmp_path, base, capsys, monkeypatch):
    for version in ["2.7.3", "2.8.0", "2.10.0"]:
        install(*package(tmp_path, version), version=version)
    (base / "current").symlink_to(base / "releases" / "2.10.0")
    monkeypatch.setattr(helper, "java_version", lambda: "11.0.32")
    monkeypatch.setattr(helper, "http_get", lambda url, timeout=3: (200, json.dumps({"version": "2.10.0"})))
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="active\n"))
    capsys.readouterr()
    helper.status(None)
    info = json.loads(capsys.readouterr().out)
    assert info["releases"] == ["2.7.3", "2.8.0", "2.10.0"]
    assert (info["current"], info["previous"], info["health"]) == ("2.10.0", "2.8.0", "/health reports 2.10.0")


@pytest.mark.parametrize("url, allowed", [
    ("https://artifactory-demo.es.nl/artifactory/demos-candidates/inventory/2.8.0/1/inventory-2.8.0.zip", True),
    ("http://10.40.0.155:8082/artifactory/demos-candidates/inventory/2.8.0/1/inventory-2.8.0.zip", True),
    ("http://artifactory-demo.es.nl/artifactory/x.zip", False),
    ("https://example.com/artifactory/x.zip", False),
    ("http://8.8.8.8:8082/artifactory/x.zip", False),
])
def test_tokens_go_only_to_jcr(url, allowed):
    assert helper.may_send_credentials(url) is allowed


def test_tomcats_error_says_which_java_it_needs():
    lines = ["27-Oct-2026 20:09:28.120 SEVERE [main] org.apache.catalina.core.StandardContext.listenerStart "
             "Error configuring application listener of class [demo.inventory.Servlets$Startup]",
             "java.lang.UnsupportedClassVersionError: demo/inventory/Servlets$Startup has been compiled by a more "
             "recent version of the Java Runtime (class file version 55.0), this version of the Java Runtime only "
             "recognizes class file versions up to 52.0 (unable to load class [demo.inventory.Servlets$Startup])",
             "27-Oct-2026 20:09:28.300 INFO [main] org.apache.catalina.startup.Catalina.start Server startup in [912] milliseconds"]
    assert helper.tomcat_error(lines) == ("UnsupportedClassVersionError: built for Java 11 (class file 55.0), "
                                          "this server runs Java 8 (up to 52.0)")
    assert helper.startup_done(lines)


def test_the_health_check_fails_fast_once_tomcat_is_up(monkeypatch, capsys):
    clock = iter(range(0, 1000))
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(helper.time, "sleep", lambda s: None)
    monkeypatch.setattr(helper, "http_get", lambda url, timeout=3: (404, ""))
    monkeypatch.setattr(helper, "tomcat_log", lambda: ["Server startup in [900] milliseconds"])
    with pytest.raises(SystemExit):
        helper.health(SimpleNamespace(expect="2.8.0", timeout=45, grace=4))
    out = capsys.readouterr().out
    assert out.startswith("FAIL /health answered 404 after ")
    assert int(out.split("after ")[1].split("s")[0]) < 45
