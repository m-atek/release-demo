"""pricing-api's canary in production: a slice of live traffic for the new
version, two CloudWatch alarms on that version alone, and a decision nobody
has to make.

The production check runs it once the production deploy has published the
new version next to live. It gives the new version its weight on the alias
live (10%, from services.yaml), creates two alarms on the metrics of that
version only (dimension ExecutedVersion): Errors and p95 Duration, starts the
load generator, and polls the alarms every few seconds for the set time.

- An alarm: the weight goes back to 0 at once, the check fails, and the
  pipeline's rollback closes the change as unsuccessful.
- No alarm for the whole time: live moves fully to the new version.

Either way the load runs a little longer so Grafana shows the result, then
stops, and both alarms are deleted. An interrupted run takes the weight back
to 0 too: the new version never keeps traffic it didn't earn.
"""

import datetime
import subprocess
import sys
import time

import pricing
import run_state


def utc(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%H:%M:%S")


def alarm_specs(function, number, app, settings):
    """put_metric_alarm arguments for both alarms, on the new version's own metrics."""
    dimensions = [{"Name": "FunctionName", "Value": function},
                  {"Name": "Resource", "Value": f"{function}:live"},
                  {"Name": "ExecutedVersion", "Value": str(number)}]
    common = {"Namespace": "AWS/Lambda", "Dimensions": dimensions, "Period": 60, "EvaluationPeriods": 1,
              "DatapointsToAlarm": 1, "TreatMissingData": "notBreaching", "ActionsEnabled": False,
              "Tags": [{"Key": "Project", "Value": "release-demo"}, {"Key": "Demo", "Value": "4-apis"}]}
    made_by = (f"pricing-api canary of {app} (Lambda version {number}) only. Made by the release pipeline "
               f"for one run and deleted after it.")
    alarms = settings["alarms"]
    return [
        {**common, "AlarmName": f"{pricing.ALARM_PREFIX}errors-v{number}", "MetricName": "Errors",
         "Statistic": "Sum", "Threshold": float(alarms["errors_per_minute"]),
         "ComparisonOperator": "GreaterThanOrEqualToThreshold",
         "AlarmDescription": f"Errors: {alarms['errors_per_minute']} or more in a minute. {made_by}"},
        {**common, "AlarmName": f"{pricing.ALARM_PREFIX}p95-v{number}", "MetricName": "Duration",
         "ExtendedStatistic": "p95", "Threshold": float(alarms["p95_duration_ms"]),
         "ComparisonOperator": "GreaterThanThreshold",
         "AlarmDescription": f"p95 Duration: over {alarms['p95_duration_ms']} ms in a minute. {made_by}"},
    ]


def describe(spec):
    return "Errors" if spec["MetricName"] == "Errors" else "p95 Duration"


def start_load(seconds, versions):
    """The load generator, as its own process, so its lines go to the same log."""
    return subprocess.Popen([sys.executable, str(pricing.HERE / "loadgen.py"), "--seconds", str(int(seconds)),
                             "--versions", ",".join(versions)], cwd=pricing.ROOT)


def stop_load(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


class Canary:
    def __init__(self, state, minutes, lam, cloudwatch, annotations=None, out=print, tell=pricing.tell,
                 load=start_load, clock=time.time, sleep=time.sleep):
        self.state, self.minutes = state, minutes
        self.lam, self.cloudwatch, self.annotations = lam, cloudwatch, annotations
        self.out, self.tell, self.load, self.clock, self.sleep = out, tell, load, clock, sleep
        self.settings = pricing.config()["canary"]
        self.function = pricing.environment("production")["function"]
        deploy = state["deploys"]["production"]
        self.new, self.old = str(deploy["number"]), str(deploy["previous_number"])
        self.new_app, self.old_app = state["version"], deploy["previous_version"]
        self.record = deploy.setdefault("canary", {})

    def annotate(self, text, tags, at, until):
        return self.annotations.start(text, tags, at=at, until=until) if self.annotations else None

    def end_annotation(self, annotation_id, at=None):
        if self.annotations:
            self.annotations.end(annotation_id, at=at)

    def run(self):
        """True when the new version earned all traffic, False when an alarm pulled it back."""
        weight = self.settings["weight_percent"]
        lead, tail = self.settings["load"]["lead_seconds"], self.settings["load"]["tail_seconds"]
        specs = alarm_specs(self.function, self.new, self.new_app, self.settings)
        names = [spec["AlarmName"] for spec in specs]
        process, window, band, decided = None, None, None, False
        try:
            for spec in specs:
                self.cloudwatch.put_metric_alarm(**spec)
            alarms = self.settings["alarms"]
            self.out(f"ok   alarms {' and '.join(names)} watch version {self.new} ({self.new_app}) only: "
                     f"Errors at {alarms['errors_per_minute']} or more a minute, p95 Duration over {alarms['p95_duration_ms']} ms")
            process = self.load(lead + self.minutes * 60 + tail + 60, [self.old_app, self.new_app])
            self.sleep(lead)  # live traffic first, all on the old version
            pricing.route(self.lam, self.function, self.old, self.new, weight / 100)
            started = self.clock()
            deadline = started + self.minutes * 60
            self.record.update({"state": "running", "weight_percent": weight, "minutes": self.minutes,
                                "started_at": started, "alarms": names})
            run_state.save(self.state)
            window = self.annotate(f"Canary: {self.new_app} at {weight}%", ["canary-window"], started, deadline)
            self.out(f"ok   canary: version {self.new} ({self.new_app}) takes {weight}% of live, "
                     f"version {self.old} ({self.old_app}) the rest, for {self.minutes} minutes from {utc(started)} UTC")
            self.tell(f"Canary: {self.new_app} takes {weight}% of live traffic for {self.minutes} minutes.")

            fired, minute = None, 0
            while self.clock() < deadline:
                states = self.cloudwatch.describe_alarms(AlarmNames=names)["MetricAlarms"]
                fired = next((a for a in states if a["StateValue"] == "ALARM"), None)
                if fired:
                    break
                if int((self.clock() - started) // 60) > minute:
                    minute = int((self.clock() - started) // 60)
                    self.out(f"ok   {minute} of {self.minutes} minutes at {weight}%: "
                             + ", ".join(f"{a['AlarmName']} {a['StateValue']}" for a in states))
                self.sleep(min(self.settings["poll_seconds"], max(0.0, deadline - self.clock())))

            if fired:
                pricing.route(self.lam, self.function, self.old)
                pulled = self.clock()
                decided = True
                self.pulled_back(fired, specs, started, pulled, weight)
                band = self.annotate(f"Alarm: {self.what(fired, specs)} on {self.new_app}", ["canary-alarm"],
                                     self.alarm_time(fired, pulled), pulled + tail)
                self.end_annotation(window, at=pulled)
            else:
                pricing.route(self.lam, self.function, self.new)
                decided = True
                self.record.update({"state": "promoted", "decided_at": self.clock()})
                self.out(f"ok   {self.minutes} minutes at {weight}% with no alarm: live moved fully to "
                         f"version {self.new} ({self.new_app})")
                self.tell(f"Canary: {self.minutes} clean minutes at {weight}%. {self.new_app} takes all traffic.")
                self.end_annotation(window)
            run_state.save(self.state)
            self.sleep(tail)  # the load goes on, so the panels show 0% or 100%
            return not fired
        finally:
            stop_load(process)
            if not decided:
                pricing.route(self.lam, self.function, self.old)
                self.record.update({"state": "interrupted", "decided_at": self.clock()})
                self.out(f"FAIL the canary stopped early: version {self.new} ({self.new_app}) is back at 0%")
                self.end_annotation(window)
                run_state.save(self.state)
            self.cloudwatch.delete_alarms(AlarmNames=names)
            self.out(f"ok   deleted the alarms {' and '.join(names)}")
            self.end_annotation(band)

    @staticmethod
    def what(fired, specs):
        return next(describe(s) for s in specs if s["AlarmName"] == fired["AlarmName"])

    def alarm_time(self, fired, fallback):
        stamp = fired.get("StateUpdatedTimestamp")
        return stamp.timestamp() if stamp else fallback

    def pulled_back(self, fired, specs, started, pulled, weight):
        what = self.what(fired, specs)
        alarm_at = self.alarm_time(fired, pulled)
        seconds = max(0, round(pulled - alarm_at))
        self.record.update({"state": "pulled-back", "decided_at": pulled,
                            "alarm": {"name": fired["AlarmName"], "what": what, "at": alarm_at,
                                      "reason": fired.get("StateReason", "")},
                            "alarm_to_zero_seconds": seconds})
        self.out(f"FAIL alarm {fired['AlarmName']} fired at {utc(alarm_at)} UTC: {what} on version {self.new} "
                 f"({self.new_app}). {fired.get('StateReason', '')}")
        self.out(f"ok   {self.new_app}'s share back to 0% at {utc(pulled)} UTC, {seconds}s after the alarm. "
                 f"{self.old_app} takes all traffic")
        self.tell(f"Canary: the {what} alarm fired on {self.new_app}. Its share of traffic was back at 0% "
                  f"{seconds} s later.")
        # The rollback's close notes start with this (scripts/run_state.py get check.notes).
        self.state["check"] = {"notes": (
            f"Canary: {self.new_app} took {weight}% of live traffic from {utc(started)} UTC. CloudWatch's {what} "
            f"alarm on its version alone fired at {utc(alarm_at)} UTC. The pipeline took its share back to 0% "
            f"at {utc(pulled)} UTC, {seconds} s after the alarm, with no one acting; {self.old_app} kept all traffic.")}
