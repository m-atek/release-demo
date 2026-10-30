"""The forty questions, read against the corpus they test."""

import rag
from conftest import STATES


def pages():
    return rag.pages_of(STATES / "2.0.0")


def test_forty_questions_twelve_twenty_and_eight():
    questions, sha256 = rag.questions()
    assert len(questions) == 40 and len(sha256) == 64
    assert [q["id"] for q in questions] == [f"Q{n:02d}" for n in range(1, 41)]
    assert questions[0]["question"] == "How do I roll back orders-service?"
    twins = [q for q in questions if q.get("twin")]
    changes = [q for q in questions if any(isinstance(s, dict) for s in q["expected_sources"])]
    assert len(twins) == 12 and len(changes) == 8 and len(questions) - len(twins) - len(changes) == 20


def test_five_smoke_questions_none_with_a_retired_twin():
    questions, _ = rag.questions()
    smoke = [q for q in questions if q.get("smoke")]
    assert len(smoke) == 5 and not any(q.get("twin") for q in smoke)
    assert rag.smoke_questions(3) == smoke[:3]


def test_each_question_has_one_to_three_facts():
    questions, _ = rag.questions()
    assert all(1 <= len(q["expected_facts"]) <= 3 for q in questions)


def test_expected_pages_are_current_and_twins_are_retired():
    found = pages()
    questions, _ = rag.questions()
    for q in questions:
        for source in q["expected_sources"]:
            if isinstance(source, str):
                assert found[source]["status"] == "current", (q["id"], source)
        if q.get("twin"):
            assert found[q["twin"]]["status"] == "retired", q["id"]


def test_eight_retired_pages_each_with_a_current_twin():
    found = pages()
    retired = {pid: page for pid, page in found.items() if page["status"] == "retired"}
    assert len(retired) == 8
    assert found["RB-02"]["replaced_by"] == "RB-07" and found["RB-07"]["title"] == "Roll back a release"
    assert "ask Pieter to put the previous package back" in found["RB-02"]["body"]
    assert "9. Ask Pieter to deploy. Only he has production access." in found["RB-02"]["body"]


def test_the_retired_checklist_is_the_main_videos_fourteen_steps():
    import html
    import re

    from common import ROOT

    page = (ROOT / "demo-data" / "release-checklist.html").read_text()
    steps = [html.unescape(re.sub(r"<[^>]+>", "", s)).strip() for s in re.findall(r"<li[^>]*>(.*?)</li>", page, re.S)]
    steps = [re.sub(r"\s+", " ", s) for s in steps if s]
    body = pages()["RB-02"]["body"]
    numbered = re.findall(r"^\d+\. (.*)$", body, re.M)
    assert len(numbered) == 14
    for step, line in zip(steps[-14:], numbered):
        assert re.sub(r"\W", "", line.replace("Handoff", "")) == re.sub(r"\W", "", step.replace("Handoff", "")), line
