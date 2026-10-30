"""The AI notes switch (videos 12 and 13): the SSM String ai-notes/enabled, off by default.

    python scripts/demos/ai_notes/switch.py on | off | status
    python3 scripts/demos/ai_notes/switch.py is-on orders-service    for the pipeline: prints on or off, never fails

Off means no evidence.py, no ai-notes build, nothing new anywhere, so the main
video, history.py and the other demos run exactly as before. On, a service
with ai_notes in services.yaml gets the change summary on its normal changes
and the likely cause after a rollback. The main reset turns it off;
reset.py ai-notes turns it on. Terraform makes the name (infra/parameters.yaml).
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

from common import SSM_PREFIX, fail, service, ssm_client  # noqa: E402


class Switch:
    """An on/off SSM String a demo turns on for its takes. Missing, unset or unreadable is off."""

    def __init__(self, name, label, applies):
        self.name, self.label, self.applies = name, label, applies

    def value(self, ssm=None):
        try:
            ssm = ssm or ssm_client()
            return ssm.get_parameter(Name=SSM_PREFIX + self.name)["Parameter"]["Value"]
        except BaseException:  # no credentials, no name, no network: off
            return None

    def is_on(self, ssm=None):
        return self.value(ssm) == "on"

    def on_for(self, service_name, ssm=None):
        """on for a service the switch applies to, while the switch is on."""
        try:
            applies = self.applies(service(service_name))
        except BaseException:
            applies = False
        return bool(applies) and self.is_on(ssm)

    def set(self, value, ssm=None):
        ssm = ssm or ssm_client()
        try:
            ssm.get_parameter(Name=SSM_PREFIX + self.name)
        except ssm.exceptions.ParameterNotFound:
            fail(f"{SSM_PREFIX}{self.name} doesn't exist yet. Apply infra/ first (scripts/tf.sh apply).")
        ssm.put_parameter(Name=SSM_PREFIX + self.name, Value=value, Type="String", Overwrite=True)
        print(f"{self.label}: {value}")

    def main(self, argv=None):
        parser = argparse.ArgumentParser(description=f"{self.label}: on, off, status, or is-on <service>")
        parser.add_argument("action", choices=["on", "off", "status", "is-on"])
        parser.add_argument("service", nargs="?")
        args = parser.parse_args(argv)
        if args.action == "is-on":
            print("on" if args.service and self.on_for(args.service) else "off")
            return 0
        if args.action == "status":
            value = self.value()
            shown = "on" if value == "on" else "off"
            print(f"{self.label}: {shown}" if value in ("on", "off") else f"{self.label}: {shown} ({value or 'not readable'})")
            return 0
        self.set(args.action)
        return 0


SWITCH = Switch("ai-notes/enabled", "AI notes", lambda config: config.get("ai_notes"))


if __name__ == "__main__":
    sys.exit(SWITCH.main())
