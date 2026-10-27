"""Put the store fleet in ServiceNow's CMDB. Idempotent.

    python scripts/demos/windows/servicenow_fleet.py

STORE-001 to STORE-006 as Windows Server CIs, each related to the application
service store-sync ("Runs on::Runs"), so a change can list them as affected
items. Run scripts/servicenow_setup.py first: it creates store-sync's CMDB
item and its rollback template from services.yaml. Signs in as the setup user.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import fail, get_parameter, service, ssm_client  # noqa: E402
from servicenow_setup import SETUP_USER, ServiceNow  # noqa: E402


def server_values(name, server, app_group):
    return {"name": name, "short_description": f"Store server, {server['store']}: runs store-sync",
            "os": "Windows 2022 Datacenter", "os_version": "10.0.20348", "operational_status": "1",
            "location": "", "support_group": app_group, "category": "Store server"}


def main():
    config = service("store-sync")
    ssm = ssm_client()
    sn = ServiceNow(get_parameter(ssm, "servicenow/instance-url"), SETUP_USER, get_parameter(ssm, "servicenow/admin-password"))
    app = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id,support_group")
    if not app:
        fail("ServiceNow has no CMDB item store-sync yet. Run: .venv/bin/python scripts/servicenow_setup.py")
    group = app["support_group"]["value"] if isinstance(app.get("support_group"), dict) else app.get("support_group", "")
    runs_on = sn.find("cmdb_rel_type", "name=Runs on::Runs", "sys_id")
    if not runs_on:
        fail("ServiceNow has no relationship type 'Runs on::Runs'.")
    table = config["servicenow"]["server_class"]
    for name, server in config["environments"]["production"]["servers"].items():
        ci = sn.ensure(table, f"name={name}", server_values(name, server, group), f"{table} {name}")
        sn.ensure("cmdb_rel_ci", f"parent={app['sys_id']}^child={ci['sys_id']}^type={runs_on['sys_id']}",
                  {"parent": app["sys_id"], "child": ci["sys_id"], "type": runs_on["sys_id"]},
                  f"  store-sync runs on {name}")
    print("Done.")


if __name__ == "__main__":
    sys.exit(main())
