import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def test_the_answer_is_an_answer_and_its_sources():
    schema = json.loads((HERE / "assistant" / "schema.json").read_text())
    assert schema["required"] == ["answer", "sources"] and schema["additionalProperties"] is False


def test_the_prompt_keeps_answers_short_and_cited():
    prompt = (HERE / "assistant" / "prompt.md").read_text()
    assert "at most three sentences" in prompt and "page id of every passage" in prompt
    assert "status" not in prompt
