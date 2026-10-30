import json
from pathlib import Path

import yaml

import chunker

HERE = Path(__file__).resolve().parents[1]
CORPUS = HERE / "corpus"


def test_the_settings_read_as_yaml_reads_them():
    assert chunker.settings(HERE / "chunking.yaml") == yaml.safe_load((HERE / "chunking.yaml").read_text())


def test_every_page_has_its_front_matter_and_a_known_status():
    pages = chunker.pages(CORPUS)
    assert pages and all(page["status"] in ("current", "retired") for page in pages)
    ids = [page["id"] for page in pages]
    assert len(ids) == len(set(ids))


def test_a_retired_page_names_a_current_page_that_replaces_it():
    pages = {page["id"]: page for page in chunker.pages(CORPUS)}
    retired = [page for page in pages.values() if page["status"] == "retired"]
    assert len(retired) == 8
    for page in retired:
        assert pages[page["replaced_by"]]["status"] == "current", page["id"]


def test_chunks_stay_under_the_limit_and_name_their_page():
    config = chunker.settings(HERE / "chunking.yaml")
    for page in chunker.pages(CORPUS):
        for chunk in chunker.chunks(page, config):
            assert chunk["text"].startswith(f"{page['id']} {page['title']}")
            assert chunker.tokens(chunk["text"]) <= config["max_tokens"] or "\n\n" not in chunk["text"]
            assert chunk["status"] == page["status"]


def test_chunk_ids_are_unique_and_stable():
    config = chunker.settings(HERE / "chunking.yaml")
    first = [c["chunk_id"] for p in chunker.pages(CORPUS) for c in chunker.chunks(p, config)]
    second = [c["chunk_id"] for p in chunker.pages(CORPUS) for c in chunker.chunks(p, config)]
    assert first == second and len(first) == len(set(first))


def test_past_changes_become_pages_of_kind_change():
    changes = json.loads((CORPUS / "changes.json").read_text())
    pages = [p for p in chunker.pages(CORPUS) if p["kind"] == "change"]
    assert len(pages) == len(changes)
    assert all(p["status"] == "current" for p in pages)
