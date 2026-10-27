"""quote-service in each cloud as a ServiceNow CMDB item, and on the change.

    python scripts/demos/clouds/servicenow_items.py create quote-service   once, after scripts/servicenow_setup.py
    python scripts/demos/clouds/servicenow_items.py link quote-service     the run's change lists all three

create makes one item per cloud from servicenow.cloud_items in services.yaml,
next to the service's own item, as the setup user. Idempotent. link runs from
the production deploy as release.pipeline: it adds the three items to the
change's affected items, so the change shows where the release went.
"""

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

import run_state  # noqa: E402
from common import get_parameter, service, ssm_client  # noqa: E402
from servicenow_setup import SETUP_USER, ServiceNow  # noqa: E402


def create(config):
    ssm = ssm_client()
    sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER,
                    get_parameter(ssm, "servicenow/admin-password"))
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")
    for cloud in config["clouds"]:
        name = config["servicenow"]["cloud_items"][cloud["key"]]
        sn.ensure("cmdb_ci_appl", f"name={name}", {
            "name": name, "short_description": f"{config['name']} in {cloud['name']} ({cloud['region']}), staging and "
                                              "production: released by the release demo pipeline. Demo system.",
            "support_group": group["sys_id"] if group else "", "operational_status": "1"}, f"CMDB item {name}")


def link(config):
    state = run_state.load()
    change = state.get("change")
    if not change:
        return
    ssm = ssm_client()
    sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), "release.pipeline",
                    get_parameter(ssm, "servicenow/release-pipeline-password"))
    for name in config["servicenow"]["cloud_items"].values():
        item = sn.find("cmdb_ci_appl", f"name={name}", "sys_id")
        if item is None:
            print(f"No CMDB item {name}: run servicenow_items.py create", file=sys.stderr)
            continue
        if not sn.find("task_ci", f"task={change['sys_id']}^ci_item={item['sys_id']}", "sys_id"):
            sn.call("POST", "/api/now/table/task_ci", {"task": change["sys_id"], "ci_item": item["sys_id"]})
    print(f"{change['number']} lists {', '.join(config['servicenow']['cloud_items'].values())}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["create", "link"])
    parser.add_argument("service")
    args = parser.parse_args()
    config = service(args.service)
    create(config) if args.command == "create" else link(config)


if __name__ == "__main__":
    main()
