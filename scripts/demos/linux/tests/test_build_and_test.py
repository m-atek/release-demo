"""The pom's version check and the merged JUnit report."""

from conftest import HERE, load

maven = load("linux_maven", HERE / "maven.py")
tests = load("linux_test", HERE / "test.py")
ROOT = HERE.parents[2]


def test_the_pom_version_is_the_projects_own():
    for version in ["2.7.3", "2.8.0"]:
        assert maven.pom_version(ROOT / "demo-states" / "inventory" / version) == version


def test_the_service_folder_holds_the_take():
    assert (ROOT / "services" / "inventory" / "VERSION").read_text().strip() == "2.8.0"


def test_surefire_reports_merge_into_one(tmp_path):
    (tmp_path / "TEST-a.xml").write_text('<testsuite name="a" tests="3" failures="0" errors="0" skipped="0"/>')
    (tmp_path / "TEST-b.xml").write_text('<testsuite name="b" tests="2" failures="1" errors="0" skipped="0"/>')
    total, failed = tests.merge_reports(tmp_path, tmp_path / "out" / "junit.xml")
    assert (total, failed) == (5, 1)
    assert 'tests="5"' in (tmp_path / "out" / "junit.xml").read_text()
