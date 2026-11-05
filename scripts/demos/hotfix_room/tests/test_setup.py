"""setup.py against a fake Jira: each issue made once, and a dry run makes nothing."""

import setup


class Jira:
    def __init__(self, there=()):
        self.issues = {summary: f"ORD-{100 + i}" for i, summary in enumerate(there)}
        self.made = []

    def search(self, jql, fields="summary"):
        return [{"key": key, "fields": {"summary": summary}} for summary, key in self.issues.items() if f'"\\"{summary}\\""' in jql]

    def create(self, kind, summary, description, labels=()):
        self.made.append((kind, summary, tuple(labels)))
        self.issues[summary] = f"ORD-{200 + len(self.made)}"
        return self.issues[summary]


def test_it_makes_the_issue_once():
    jira = Jira()
    assert setup.issue(jira, "Bug", setup.HOTFIX_SUMMARY, "text", "catalog", False) == ("ORD-201", "made now")
    assert setup.issue(jira, "Bug", setup.HOTFIX_SUMMARY, "text", "catalog", False) == ("ORD-201", "there already")
    assert jira.made == [("Bug", setup.HOTFIX_SUMMARY, ("catalog",))]


def test_a_dry_run_makes_nothing():
    jira = Jira()
    assert setup.issue(jira, "Story", setup.ROOM_SUMMARY, "text", "forecast", True) == ("ORD-?", "would make it")
    assert jira.made == []


def test_a_summary_that_only_resembles_isnt_the_issue():
    jira = Jira(["Supplier search finds nothing in lower case, again"])
    jira.search = lambda jql, fields="summary": [{"key": "ORD-100", "fields": {"summary": "Supplier search finds nothing in lower case, again"}}]
    assert setup.found_issue(jira, setup.HOTFIX_SUMMARY) is None


def test_the_two_issues_are_the_ones_the_reset_looks_for():
    assert [item[1] for item in setup.ISSUES] == [setup.HOTFIX_SUMMARY, setup.ROOM_SUMMARY]
    assert [item[3] for item in setup.ISSUES] == ["catalog", "forecast"]     # the reset goes by these
    assert "setup.ISSUES" in (setup.HERE / "reset.py").read_text()


def test_no_summary_says_planted():
    for _, summary, description, _, _ in setup.ISSUES:
        for word in ("planted", "staged", "demo"):
            assert word not in summary and word not in description
