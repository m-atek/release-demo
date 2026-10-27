"""Demo 3 in ServiceNow: the four Linux Server CIs and their locations. Idempotent.

    python scripts/demos/linux/servicenow_setup.py

Run it from the Mac after scripts/servicenow_setup.py, which makes the
inventory application CI and its change templates from services.yaml. This
adds inv-cloud-01 and inv-cloud-02 in "AWS eu-west-1 (demo)" and inv-dc-01 and
inv-dc-02 in "Data center (demo)", each running inventory. The deploy lists
them on every inventory change as affected CIs, with each server's result in
a work note.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import get_parameter, service, ssm_client  # noqa: E402
from servicenow_setup import SETUP_USER, ServiceNow  # noqa: E402

LOCATIONS = {"cloud": "AWS eu-west-1 (demo)", "data-center": "Data center (demo)"}


def main():
    ssm = ssm_client()
    sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER, get_parameter(ssm, "servicenow/admin-password"))
    config = service("inventory")
    app = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id")
    if app is None:
        sys.exit("No inventory CI yet: run scripts/servicenow_setup.py first")
    group = sn.find("sys_user_group", "name=Platform team", "sys_id")
    locations = {site: sn.ensure("cmn_location", f"name={name}", {"name": name}, f"location {name}")["sys_id"]
                 for site, name in LOCATIONS.items()}
    runs_on = sn.find("cmdb_rel_type", "name=Runs on::Runs", "sys_id")
    for server in config["environments"]["production"]["servers"]:
        name = server["name"]
        record = sn.ensure("cmdb_ci_linux_server", f"name={name}", {
            "name": name, "location": locations[server["site"]], "os": "Linux Ubuntu", "os_version": "24.04",
            "short_description": f"inventory server, {LOCATIONS[server['site']]}. Demo system.",
            "support_group": group["sys_id"] if group else "", "operational_status": "1"}, f"Linux Server {name}")
        if runs_on:
            sn.ensure("cmdb_rel_ci", f"parent={app['sys_id']}^child={record['sys_id']}^type={runs_on['sys_id']}",
                      {"parent": app["sys_id"], "child": record["sys_id"], "type": runs_on["sys_id"]},
                      f"  inventory runs on {name}")
    print("Done.")


if __name__ == "__main__":
    main()
