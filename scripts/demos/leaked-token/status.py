"""scripts/demo.sh status leaked-token: the pipeline's JFrog token by id and age, and any open leak incident."""

import sys

import take  # noqa: F401
from common import get_parameter, ssm_client


def main():
    ssm = ssm_client()
    print(f"  jcr/token: {take.describe(get_parameter(ssm, 'jcr/token'))} (the value is never shown)")
    try:
        from change import connect
        from rotators import records

        sn, _ = connect()
        incidents = records.open_leak_incidents(sn)
        print("  leak incidents open: " + (", ".join(i["number"] for i in incidents) if incidents else "none"))
    except SystemExit:
        print("  ServiceNow didn't answer (asleep?): leak incidents unknown")
    print("  cost: nothing by the hour beyond the tools host")
    return 0


if __name__ == "__main__":
    sys.exit(main())
