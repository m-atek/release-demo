"""Set up Jamf for demos 6 and 7: the groups and the Store Assistant policy. Idempotent.

    python scripts/demos/desktop/jamf_setup.py              the stand-in (STANDIN_JAMF=0 for the Jamf Pro trial)

Creates what the pipeline expects, through the same API calls on the real
Jamf Pro and on the stand-in:

- the smart computer groups Pilot (Computer Name like "pilot") and All
  managed Macs (no criteria: every managed Mac);
- the policy Store Assistant: recurring check-in, once per computer, an
  inventory update after it runs, scoped to Pilot. The pipeline sets its
  package;
- for demo 7, the smart mobile device groups Pilot and All store devices.

On the stand-in it also adds agentless store devices, so each group has
members beyond the pilot Mac: five Macs on Store Assistant 1.6.0, and iPads
for demo 7. On the real Jamf Pro, the push certificate, the API client and
the pilot Mac's enrollment are UI steps (README, Demo on a call: desktop).
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop  # noqa: E402
from desktop import service  # noqa: E402

STORE_MACS = [f"store-mac-{n:02d}" for n in range(2, 7)]
STORE_IPADS = ["pilot-ipad-01"] + [f"store-ipad-{n:02d}" for n in range(2, 7)]


def baseline_apps(app, version):
    return [{"name": app["name"], "version": version, "bundleId": app["bundle_id"],
             "path": f"/Applications/{app['name']}.app"}]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", default="1.6.0", help="the version the agentless Macs start on")
    args = parser.parse_args()

    config = service("store-assistant")
    app = desktop.app_config(config)
    client = desktop.jamf(config)
    jamf = config["jamf"]
    print(f"Jamf: {'the stand-in' if client.standin else 'Jamf Pro'}")

    for name, criteria in ((jamf["pilot_group"], [("Computer Name", "like", "pilot")]), (jamf["all_group"], [])):
        if client.computer_group(name):
            print(f"  computer group {name}: already there")
        else:
            client.create_smart_computer_group(name, criteria)
            print(f"  computer group {name}: created")
    if client.policy(jamf["policy"]):
        print(f"  policy {jamf['policy']}: already there")
    else:
        client.create_policy(jamf["policy"], [jamf["pilot_group"]], recon=True)
        print(f"  policy {jamf['policy']}: created, recurring check-in, once per computer, scoped to {jamf['pilot_group']}")
    for name, criteria in (("Pilot", [("Display Name", "like", "pilot")]), ("All store devices", [])):
        if client.mobile_device_group(name):
            print(f"  mobile device group {name}: already there")
        else:
            client.create_smart_mobile_group(name, criteria)
            print(f"  mobile device group {name}: created")

    if client.standin:
        for name in STORE_MACS:
            reply = client.add_simulated_device("computer", name, baseline_apps(app, args.baseline))
            print(f"  {name}: {'already there' if reply['existing'] else 'added, on ' + args.baseline}")
        for name in STORE_IPADS:
            reply = client.add_simulated_device("mobile_device", name)
            print(f"  {name}: {'already there' if reply['existing'] else 'added'}")
    print("Jamf is set up. Enroll the pilot Mac: scripts/demos/desktop/pilot_setup.sh <user>@<pilot Mac>")


if __name__ == "__main__":
    main()
