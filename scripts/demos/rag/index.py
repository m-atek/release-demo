"""A version's search index, made only when its chunk hash has none.

    python scripts/demos/rag/index.py ensure <package folder>    the index name, once it is online

For a chunk hash without a table:
1. uploads the package's corpus and chunking.yaml to the volume,
   /Volumes/<catalog>/runbook_assistant/packages/<sha256>/, and chunker.py
   to the workspace, where a job's Python script task reads it;
2. submits a one-time serverless job run of chunker.py as the service
   principal, which merges `pages` and writes chunks_<hash8> with change
   data feed on;
3. creates the Delta Sync index on that table, with managed embeddings from
   databricks-gte-large-en, and waits for it, up to 15 minutes.
A table that exists already gets its index made or reused. All three
demo versions share one chunk hash, so one table and one index.
"""

import argparse
import base64
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402
from common import fail  # noqa: E402
from databricks_api import Databricks  # noqa: E402

WAIT_SECONDS = 15 * 60


def endpoint_online(db, settings=None):
    settings = settings or rag.databricks_settings()
    found = db.endpoint(settings["endpoint"])
    state = ((found or {}).get("endpoint_status") or {}).get("state")
    return found if state == "ONLINE" else None


def upload_corpus(db, folder, sha256, catalog_name):
    settings = rag.databricks_settings()
    base = f"/Volumes/{catalog_name}/{settings['schema']}/{settings['volume']}/{sha256}"
    for path in [folder / "chunking.yaml", *sorted(p for p in (folder / "corpus").rglob("*") if p.is_file())]:
        db.upload(f"{base}/{path.relative_to(folder).as_posix()}", path.read_bytes())
    script = f"/Shared/runbook-assistant-jobs/{sha256[:16]}/chunker.py"
    db.call("POST", "/api/2.0/workspace/mkdirs", {"path": script.rsplit("/", 1)[0]})
    db.call("POST", "/api/2.0/workspace/import", {"path": script, "format": "AUTO", "overwrite": True,
                                                  "content": base64.b64encode((folder / "chunker.py").read_bytes()).decode()})
    return f"{base}/corpus", script


def run_chunker(db, corpus, script, catalog_name, table, clock=time.monotonic, sleep=time.sleep):
    settings = rag.databricks_settings()
    run_id = db.submit_run({
        "run_name": f"runbook-assistant {table}",
        "tasks": [{"task_key": "chunks", "environment_key": "default",
                   "spark_python_task": {"python_file": f"/Workspace{script}",
                                         "parameters": [corpus, catalog_name, settings["schema"], table]}}],
        "environments": [{"environment_key": "default",
                          "spec": {"environment_version": str(settings.get("serverless_environment", "4"))}}],
    })
    print(f"  chunker job run {run_id} for {table}", flush=True)
    deadline = clock() + WAIT_SECONDS
    while clock() < deadline:
        state = db.run(run_id).get("state") or {}
        if state.get("life_cycle_state") in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
            if state.get("result_state") == "SUCCESS":
                return run_id
            fail(f"The chunker job failed: {state.get('result_state')} {state.get('state_message', '')}".strip())
        sleep(10)
    fail(f"The chunker job didn't finish in {WAIT_SECONDS // 60} minutes.")


def wait_online(db, name, clock=time.monotonic, sleep=time.sleep, timeout=WAIT_SECONDS):
    deadline = clock() + timeout
    while clock() < deadline:
        found = db.index(name) or {}
        if (found.get("status") or {}).get("ready"):
            return found
        sleep(10)
    fail(f"The index {name} isn't online after {timeout // 60} minutes: {(found.get('status') or {}).get('message', '')}")


def ensure(folder, db=None, catalog_name=None, sha256=None):
    """The index for the package in folder, made if missing; its name."""
    folder = Path(folder)
    db = db or Databricks.as_pipeline()
    settings = rag.databricks_settings()
    if not endpoint_online(db, settings):
        raise SystemExit(rag.ENDPOINT_DOWN)  # the message is the exit's, for /ask and Slack
    catalog_name = catalog_name or rag.catalog()
    chunk = rag.chunk_hash(folder, settings["embedding_model"])
    table, name = rag.table_name(chunk, catalog_name=catalog_name), rag.index_name(chunk, catalog_name=catalog_name)
    if not db.table(table):
        corpus, script = upload_corpus(db, folder, sha256 or chunk, catalog_name)
        run_chunker(db, corpus, script, catalog_name, table.rsplit(".", 1)[1])
    if not db.index(name):
        db.create_index(name, settings["endpoint"], table, settings["embedding_model"], rag.INDEX_COLUMNS)
        print(f"  index {name}: created, embedding with {settings['embedding_model']}", flush=True)
    wait_online(db, name)
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ensure").add_argument("folder", type=Path)
    args = parser.parse_args()
    print(json.dumps({"index": ensure(args.folder)}))


if __name__ == "__main__":
    main()
