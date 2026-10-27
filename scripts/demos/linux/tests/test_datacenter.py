"""The data-center job runs only requests it should: dc servers, inventory packages from JCR."""

import pytest

import datacenter

SHA = "a" * 64
URL = "https://artifactory-demo.es.nl/artifactory/demos-candidates/inventory/2.8.0/57/inventory-2.8.0.zip"


def request(**extra):
    vars_ = {"rollout_action": "deploy", "version": "2.8.0", "package": {"source": "jcr", "url": URL, "sha256": SHA}}
    vars_.update(extra)
    return {"playbook": "release.yml", "names": ["inv-dc-01", "inv-dc-02"], "extra_vars": vars_, "tags": None}


def test_a_deploy_from_jcr_passes():
    assert datacenter.checked(request())["names"] == ["inv-dc-01", "inv-dc-02"]


@pytest.mark.parametrize("change", [
    {"playbook": "site.yml"},
    {"names": ["inv-cloud-01"]},
    {"names": []},
    {"tags": "all"},
])
def test_other_playbooks_servers_or_tags_are_refused(change):
    with pytest.raises(SystemExit):
        datacenter.checked({**request(), **change})


@pytest.mark.parametrize("url", [
    "https://evil.example/artifactory/demos-candidates/inventory/2.8.0/57/inventory-2.8.0.zip",
    "http://artifactory-demo.es.nl/artifactory/demos-candidates/inventory/2.8.0/57/inventory-2.8.0.zip",
    "https://artifactory-demo.es.nl/artifactory/demos-candidates/catalog/1.9.0/3/catalog-1.9.0.zip",
])
def test_the_tokens_go_nowhere_but_jcr(url):
    with pytest.raises(SystemExit):
        datacenter.checked(request(package={"source": "jcr", "url": url, "sha256": SHA}))


def test_a_package_needs_its_checksum():
    with pytest.raises(SystemExit):
        datacenter.checked(request(package={"source": "jcr", "url": URL, "sha256": "short"}))


def test_a_local_file_only_from_the_mac(tmp_path):
    inside = {"source": "file", "file": str(tmp_path / "package" / "inventory-2.7.3.zip"), "sha256": SHA}
    with pytest.raises(SystemExit):
        datacenter.checked(request(package=inside))
    assert datacenter.checked(request(package=inside), local_root=tmp_path)


def test_unknown_settings_and_actions_are_refused():
    with pytest.raises(SystemExit):
        datacenter.checked(request(become_user="root"))
    with pytest.raises(SystemExit):
        datacenter.checked(request(rollout_action="destroy"))
    with pytest.raises(SystemExit):
        datacenter.checked(request(java_drift_servers=["inv-cloud-01"]))
