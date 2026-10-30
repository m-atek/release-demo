"""The billed part of demo 23: the AI Search endpoint and its indexes.

    scripts/demo.sh up rag        the endpoint and the indexes the pointers name, and 2.0.0's: "Ready"
    scripts/demo.sh down rag      every index on the endpoint, then the endpoint
    scripts/demo.sh status rag    the endpoint, its indexes, the hours since it was made, the warehouse

The endpoint bills by the hour from its first index (4 DBU an hour, about
USD 0.28 at the US East rate), and nothing once no index has existed on it
for 24 hours: so down deletes it. The pages and chunk tables, the volume,
the experiment and the warehouse stay, and up rebuilds the indexes from the
tables. On the Mac this runs as you (the Databricks CLI profile
release-demo); on the tools host, where its idle stop runs down, as the
service principal, which up makes manager of the endpoint.
"""

import argparse
import datetime
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402
from common import ROOT, fail, get_parameter, ssm_client  # noqa: E402
from databricks_api import Databricks, environment  # noqa: E402

UP_TIMEOUT = 25 * 60
BASELINE = "2.0.0"


def client(identity):
    return Databricks.as_pipeline() if identity == "pipeline" else Databricks.as_you()


def wanted_indexes(ssm, catalog_name):
    """The indexes the two pointers name, and 2.0.0's, from its demo state's chunk hash."""
    names = []
    for env in ("staging", "production"):
        pointer = rag.read_pointer(env, ssm)
        if pointer and pointer["index"] not in names:
            names.append(pointer["index"])
    baseline = rag.index_name(rag.chunk_hash(ROOT / "demo-states" / rag.SERVICE / BASELINE), catalog_name=catalog_name)
    if baseline not in names:
        names.append(baseline)
    return names


def wait(what, check, timeout, interval=15):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        found = check()
        if found:
            print(f"  {what:<46} ok ({time.monotonic() - started:.0f}s)", flush=True)
            return found
        time.sleep(interval)
    fail(f"  {what}: not after {timeout // 60} minutes")


def up(args):
    started = time.monotonic()
    ssm = ssm_client()
    db = client(args.identity)
    settings = rag.databricks_settings()
    catalog_name = rag.catalog(ssm)
    if not db.endpoint(settings["endpoint"]):
        db.create_endpoint(settings["endpoint"])
        print(f"  endpoint {settings['endpoint']}: created (Standard)")

    def online():
        found = db.endpoint(settings["endpoint"])
        return found if ((found or {}).get("endpoint_status") or {}).get("state") == "ONLINE" else None

    endpoint = wait(f"endpoint {settings['endpoint']} online", online, UP_TIMEOUT)
    if args.identity == "you":
        db.endpoint_permissions(endpoint["id"], get_parameter(ssm, "databricks/client-id"), "CAN_MANAGE")
        print("  release-pipeline: may use and delete the endpoint (CAN_MANAGE)")
    for name in wanted_indexes(ssm, catalog_name):
        table = name.removesuffix("_index")
        if not db.table(table):
            print(f"  {table}: no table yet, so no index (the first release makes both)")
            continue
        if not db.index(name):
            db.create_index(name, settings["endpoint"], table, settings["embedding_model"], rag.INDEX_COLUMNS)
        wait(f"index {name.rsplit('.', 1)[1]} online",
             lambda name=name: ((db.index(name) or {}).get("status") or {}).get("ready"), UP_TIMEOUT)
    took = time.monotonic() - started
    print(f"Ready in {int(took // 60)}m {int(took % 60):02d}s. The endpoint bills by the hour from its first index, "
          "about USD 0.28 an hour: scripts/demo.sh down rag ends it.")


def down(args):
    db = client(args.identity)
    settings = rag.databricks_settings()
    if not db.endpoint(settings["endpoint"]):
        print(f"No endpoint {settings['endpoint']}: nothing bills by the hour.")
        return
    for index in db.indexes(settings["endpoint"]):
        db.delete_index(index["name"])
        print(f"  index {index['name']}: deleted")
    db.delete_endpoint(settings["endpoint"])
    print(f"Endpoint {settings['endpoint']} deleted. The tables, volume, experiment and warehouse stay; "
          "scripts/demo.sh up rag makes the indexes again.")


def status(args):
    db = client(args.identity)
    settings = rag.databricks_settings()
    endpoint = db.endpoint(settings["endpoint"])
    if not endpoint:
        print(f"endpoint {settings['endpoint']}: none (nothing bills by the hour)")
    else:
        created = endpoint.get("creation_timestamp")
        hours = (time.time() - created / 1000) / 3600 if created else 0
        state = (endpoint.get("endpoint_status") or {}).get("state")
        print(f"endpoint {settings['endpoint']}: {state}, made {hours:.1f} hours ago")
        for index in db.indexes(settings["endpoint"]):
            found = db.index(index["name"]) or {}
            ready = "online" if (found.get("status") or {}).get("ready") else "not ready"
            print(f"  index {index['name']}: {ready}, {(found.get('status') or {}).get('indexed_row_count', '?')} rows")
    warehouse = db.warehouse(settings["warehouse"])
    print(f"warehouse {settings['warehouse']}: {(warehouse or {}).get('state', 'none')}")
    for env in ("staging", "production"):
        pointer = rag.read_pointer(env)
        print(f"{env}: {pointer['version'] + ' on ' + pointer['index'].rsplit('.', 1)[1] if pointer else 'nothing yet'}")
    print(f"at {datetime.datetime.now():%H:%M:%S}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    parser.add_argument("--as", dest="identity", choices=["you", "pipeline"], default=environment(),
                        help="whose Databricks identity: yours on the Mac, the pipeline's on the tools host")
    args = parser.parse_args()
    {"up": up, "down": down, "status": status}[args.action](args)


if __name__ == "__main__":
    main()
