"""Video 27's fill: the quiet day before a take, seeded and not measured.

    python scripts/demos/error_budget/seed.py              the last 24 hours, up to this minute
    python scripts/demos/error_budget/seed.py --dry-run    what it would write, and nothing else
    python scripts/demos/error_budget/seed.py --check      what CloudWatch shows of it now

Mateusz's decision, 2 November 2026: only the fill is mocked, so the video can be
filmed the same day. The load generator still runs from before the takes, so every
request and failure from the end of the seed on is real: the failures of the bad
release, the refusal, the budget spent. Only the quiet day before them is made up.

AWS lets nobody write into AWS/Lambda, so this writes one metric of its own, whose
name says what it is: ReleaseDemo/ErrorBudget SeededRequests, FunctionName
orders-service-production, about 60 a minute, slightly uneven like a real
generator's counts, and nothing for failures. budget.py and the dashboard read each
minute once: a minute with a seeded count reads that count and 0 failed, any other
minute what AWS/Lambda counted. So a real failure inside the seeded part doesn't
count, whether it came from another video or from an earlier take.

Each minute's count follows from the minute itself and is read with Maximum, so a
second run changes nothing it wrote before. reset.py error-budget runs the same
write every time, up to the minute it ends, so a take never needs a fill older than
its reset, and a failed take can be reset and taken again the same hour.

CloudWatch's rules, from PutMetricData's page: a data point from 24 hours ago or
longer can take at least 48 hours to show, so the oldest minute written here stays
MARGIN_MINUTES inside that. One from 3 to 24 hours ago can take as much as 2 hours
to show: --check says how much it shows, and the take waits for it. CloudWatch
deletes nothing on request: 60-second data stays for 15 days. Nothing outside this
demo reads this metric.
"""

import argparse
import datetime
import hashlib
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(1, str(HERE.parents[1]))

import budget  # noqa: E402
from common import aws_session, service  # noqa: E402

SERVICE = "orders-service"
# A data point 24 hours old or older can take at least 48 hours to show.
MARGIN_MINUTES = 3
# PutMetricData takes 1000 metrics a call.
BATCH = 500
# What a take needs: 0.1% of this is 86.
FULL_DAY = range(86_000, 87_000)


def count_for(minute):
    """About 60, slightly uneven like a real generator's: the same every time for the same minute."""
    byte = hashlib.sha256(str(minute).encode()).digest()[0]
    return 59 if byte < 26 else 61 if byte >= 230 else 60


def minutes_to_seed(now, hours):
    """Every whole minute of the window, up to the minute it is now, none of them 24 hours old yet."""
    end = int(now // 60) * 60
    return list(range(end - hours * 3600 + MARGIN_MINUTES * 60, end, 60))


def describe(minutes):
    requests = sum(count_for(minute) for minute in minutes)
    return (f"seed {len(minutes):,} minutes, {requests:,} requests, none failed, "
            f"{budget.clock(minutes[0])} yesterday to {budget.clock(minutes[-1] + 60)}")


def write(client, settings, minutes):
    """The counts, as one metric of this demo's own: never into AWS/Lambda."""
    seed = settings["seed"]
    dimensions = [{"Name": "FunctionName", "Value": settings["function"]}]
    for start in range(0, len(minutes), BATCH):
        client.put_metric_data(Namespace=seed["namespace"], MetricData=[
            {"MetricName": seed["metric"], "Dimensions": dimensions, "Unit": "Count", "Value": float(count_for(minute)),
             "Timestamp": datetime.datetime.fromtimestamp(minute, datetime.timezone.utc)}
            for minute in minutes[start:start + BATCH]])


def check(client, settings, now, out=print):
    """What CloudWatch shows now. Exit 0 once the window holds a full day and nothing failed."""
    reading = budget.take_reading(client, settings, now)
    out(budget.head(reading, f"Spent: {reading.spent}%."))
    out(budget.seeded_line(reading))
    ready = reading.requests in FULL_DAY and reading.failed == 0
    out("Ready: a full day, nothing failed." if ready else
        "Not yet: CloudWatch can take up to 2 hours to show data points 3 to 24 hours old.")
    return 0 if ready else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print what it would write, change nothing")
    parser.add_argument("--check", action="store_true", help="what CloudWatch shows of it now; exit 0 when it reads a full day")
    args = parser.parse_args()
    settings = service(SERVICE)["error_budget"]
    now = time.time()
    if args.check:
        return check(aws_session().client("cloudwatch"), settings, now)
    minutes = minutes_to_seed(now, settings["window_hours"])
    if args.dry_run:
        print(f"Would {describe(minutes)}: {settings['seed']['namespace']} {settings['seed']['metric']}.")
        return 0
    write(aws_session().client("cloudwatch"), settings, minutes)
    print(f"Done: {describe(minutes)}. scripts/demos/error_budget/seed.py --check says how much CloudWatch shows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
