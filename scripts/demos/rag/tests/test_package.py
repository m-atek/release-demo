"""The three demo states, their package and the one index they share."""

import json
import zipfile

import build
import rag
from conftest import STATES


def test_the_three_versions_share_one_chunk_hash():
    hashes = {rag.chunk_hash(STATES / v) for v in ("2.0.0", "2.1.0", "2.1.1")}
    assert len(hashes) == 1


def test_a_new_way_of_chunking_gets_a_table_of_its_own(tmp_path):
    import shutil

    copy = tmp_path / "state"
    shutil.copytree(STATES / "2.0.0", copy)
    before = rag.chunk_hash(copy)
    (copy / "chunking.yaml").write_text((copy / "chunking.yaml").read_text().replace("400", "300"))
    assert rag.chunk_hash(copy) != before
    assert rag.chunk_hash(STATES / "2.0.0", "another-model") != rag.chunk_hash(STATES / "2.0.0")


def test_the_index_is_named_after_the_hash():
    chunk = rag.chunk_hash(STATES / "2.0.0")
    assert rag.index_name(chunk, catalog_name="demo") == f"demo.runbook_assistant.chunks_{chunk[:8]}_index"


def test_the_same_source_gives_the_same_package(tmp_path):
    one = build.build("runbook-assistant", folder=STATES / "2.1.0", out=tmp_path / "a", commit="abc")
    two = build.build("runbook-assistant", folder=STATES / "2.1.0", out=tmp_path / "b", commit="abc")
    assert one["sha256"] == two["sha256"] and one["chunk_hash"] == rag.chunk_hash(STATES / "2.1.0")
    assert (one["model"], one["effort"], one["embedding_model"]) == ("claude-haiku-5-5", "low", "databricks-gte-large-en")
    with zipfile.ZipFile(tmp_path / "a" / "runbook-assistant" / one["package"]) as archive:
        names = archive.namelist()
    assert "assistant/retriever.py" in names and "corpus/pages/RB-02.md" in names and "requirements.txt" in names
    assert not any(name.startswith("tests/") for name in names)
    written = json.loads((tmp_path / "a" / "runbook-assistant" / "build-info.json").read_text())
    assert written["sha256"] == one["sha256"]


def test_the_diff_of_2_1_0_swaps_one_line_and_2_1_1_adds_it_back():
    import difflib

    def changed(a, b):
        lines = difflib.unified_diff((STATES / a / "assistant/retriever.py").read_text().splitlines(),
                                     (STATES / b / "assistant/retriever.py").read_text().splitlines(), lineterm="", n=0)
        return [line for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---"))]

    assert changed("2.0.0", "2.1.0") == ['-        filters={"status": "current"},', '+        query_type="hybrid",']
    assert changed("2.1.0", "2.1.1") == ['+        filters={"status": "current"},']


def test_only_the_retriever_tests_version_and_changelog_differ():
    import filecmp

    for a, b in (("2.0.0", "2.1.0"), ("2.1.0", "2.1.1")):
        compare = filecmp.dircmp(STATES / a, STATES / b)
        different = set(compare.diff_files)
        for sub in compare.subdirs.values():
            different |= {f"{sub.left.split('/')[-1]}/{name}" for name in sub.diff_files}
        assert different == {"VERSION", "CHANGELOG.md", "assistant/retriever.py", "tests/test_retriever.py"}


def test_the_service_folder_holds_2_0_0():
    from common import ROOT

    assert (ROOT / "services" / "runbook-assistant" / "VERSION").read_text().strip() == "2.0.0"
    assert rag.chunk_hash(ROOT / "services" / "runbook-assistant") == rag.chunk_hash(STATES / "2.0.0")
