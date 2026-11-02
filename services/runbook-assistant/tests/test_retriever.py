import yaml

from assistant import retriever

SETTINGS = {"num_results": 5}


def test_search_asks_for_the_question():
    assert retriever.search_arguments("How do I roll back orders-service?", SETTINGS)["query_text"] == \
        "How do I roll back orders-service?"


def test_search_returns_as_many_passages_as_the_settings_say():
    assert retriever.search_arguments("q", SETTINGS)["num_results"] == 5
    assert retriever.search_arguments("q", {"num_results": 3})["num_results"] == 3


def test_search_reads_what_an_answer_needs():
    columns = retriever.search_arguments("q", SETTINGS)["columns"]
    assert {"chunk_id", "page_id", "title", "status", "text"} <= set(columns)


def test_the_package_asks_for_five_passages():
    from pathlib import Path

    settings = yaml.safe_load((Path(__file__).resolve().parents[1] / "assistant.yaml").read_text())
    assert settings["num_results"] == 5


def test_search_is_hybrid_so_service_names_match_exactly():
    assert retriever.search_arguments("ORD-12", SETTINGS)["query_type"] == "hybrid"


def test_search_asks_for_current_pages_only():
    assert retriever.search_arguments("q", SETTINGS)["filters"] == {"status": "current"}
