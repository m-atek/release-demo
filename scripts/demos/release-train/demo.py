"""Switch demo 21's release train on or off, and see where it stands.

    python scripts/demos/release-train/demo.py up        scripts/demo.sh up release-train
    python scripts/demos/release-train/demo.py down      scripts/demo.sh down release-train
    python scripts/demos/release-train/demo.py status    scripts/demo.sh status release-train

up enables the EventBridge Scheduler schedule that starts the tools host 30
minutes before the train's first departure (services.yaml, train.departures,
Europe/Amsterdam, weekdays), and switches the train on (SSM
release-train/enabled), so the job's timer run at a departure leaves. down
disables the schedule and switches the train off. The job's timer itself
always ticks: Job DSL would switch a disabled job back on at the host's next
boot, so the switch lives in SSM, where train.py reads it. Idle stop powers
the host off after the train, as after any run.
"""

import datetime
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parents[1]))

import flags  # noqa: E402
from common import aws_session, get_parameter, put_parameter, service, ssm_client  # noqa: E402

SCHEDULE = "release-demo-train-tools-host"
ALARM = "orders-service-production-errors"
TIME_ZONE = "Europe/Amsterdam"
LEAD_MINUTES = 30


def departures():
    return sorted(service(flags.SERVICE)["train"]["departures"])


def start_expression(first):
    """The schedule's cron, LEAD_MINUTES before the first departure, on weekdays."""
    hour, minute = (int(part) for part in first.split(":"))
    start = datetime.datetime(2026, 1, 5, hour, minute) - datetime.timedelta(minutes=LEAD_MINUTES)
    return f"cron({start.minute} {start.hour} ? * MON-FRI *)", start.strftime("%H:%M")


def set_schedule(enabled):
    scheduler = aws_session().client("scheduler")
    current = scheduler.get_schedule(Name=SCHEDULE)
    expression, at = start_expression(departures()[0])
    keep = {key: current[key] for key in ("FlexibleTimeWindow", "Target") if key in current}
    scheduler.update_schedule(Name=SCHEDULE, ScheduleExpression=expression, ScheduleExpressionTimezone=TIME_ZONE,
                              State="ENABLED" if enabled else "DISABLED", Description=current.get("Description", ""),
                              **keep)
    return at


def up():
    at = set_schedule(True)
    put_parameter(ssm_client(), "release-train/enabled", "on", secure=False)
    print(f"Release train on: the tools host starts at {at} on weekdays, and the train leaves at "
          f"{', '.join(departures())} ({TIME_ZONE}). scripts/demo.sh down release-train switches both off.")


def down():
    set_schedule(False)
    put_parameter(ssm_client(), "release-train/enabled", "off", secure=False)
    print("Release train off: the schedule is disabled and the timer runs leave nothing.")


def status():
    scheduler = aws_session().client("scheduler")
    current = scheduler.get_schedule(Name=SCHEDULE)
    print(f"schedule {SCHEDULE}: {current['State']}, {current['ScheduleExpression']} {current.get('ScheduleExpressionTimezone', '')}")
    print(f"train: {get_parameter(ssm_client(), 'release-train/enabled')}, departures {', '.join(departures())}")
    alarm = aws_session().client("cloudwatch").describe_alarms(AlarmNames=[ALARM])["MetricAlarms"]
    print(f"alarm {ALARM}: {alarm[0]['StateValue'] if alarm else 'missing'}")
    for environment in ("staging", "production"):
        values = flags.production_values(environment)
        shown = ", ".join(f"{name} {'on' if on else 'off'}" for name, on in values.items())
        print(f"flags in {environment}: {shown or 'none deployed'}")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"up": up, "down": down, "status": status}[command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
