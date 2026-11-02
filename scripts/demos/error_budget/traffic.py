"""Video 27's load generator: one request a second to orders-service production,
as the Lambda function release-demo-error-budget-traffic (infra/demos/27-error-budget).

An EventBridge rule starts it every minute, always. It reads the SSM String
error-budget/traffic-until first and exits at once when that is "off", unset or
past, so an idle day costs a fraction of a cent. scripts/demo.sh up error-budget
sets it to now + 72 hours, down to "off". No reset touches it.

While on, each run sends 60 GETs, one a second, through the public function URL
(TARGET_URL), with a 5-second timeout, in a fixed rotation of 20 positioned from
the clock, so it needs no state: /api/orders 12 times, / twice, and
/api/price?product=<name> once for each of the six products. So exactly one
request in 20, "Sports socks, 3 pairs", fails on 1.27.0, and that stays 3 a
minute, under the 5 a minute of video 21's alarm. Whichever version is live
answers: /api/price answers 404 on versions before 1.26.0, which isn't a failure.

Each run ends with one summary line to Grafana Cloud Logs (labels
source=release-demo-loadgen, service=orders-service, environment=production,
kind=traffic): {"requests": 60, "failed": 3, "by_status": {...}, "failed_paths":
{...}}. Failed means 5xx or no answer. It is the second count, from the caller's
side, that the acceptance checks compare with CloudWatch.

Standard library and boto3, which Lambda's runtime has. This file is the whole
package: it imports nothing from the repository.
"""

import base64
import concurrent.futures
import datetime
import http.client
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

SSM_PREFIX = "/release-demo/"
SWITCH = "error-budget/traffic-until"
PER_RUN = 60
REQUEST_TIMEOUT = 5.0
# What the end of a run keeps back from the function's 65-second timeout, for the summary.
SUMMARY_MARGIN_SECONDS = 2.0
USER_AGENT = "release-demo error budget traffic"
PRODUCTS = ["Running shoes", "Rain jacket", "Day backpack", "Water bottle", "Training shirt", "Sports socks, 3 pairs"]
LABELS = {"source": "release-demo-loadgen", "service": "orders-service", "environment": "production", "kind": "traffic"}
# Built once per container: at 128 MB, loading the CA bundle for every request would cost more than the request.
TLS = ssl.create_default_context()


def price(product):
    return "/api/price?product=" + urllib.parse.quote(product)


# A fixed rotation of 20: the failing product comes once per 20 seconds, evenly.
ROTATION = [
    "/api/orders", "/api/orders", price(PRODUCTS[0]), "/api/orders", "/",
    "/api/orders", "/api/orders", price(PRODUCTS[1]), "/api/orders", price(PRODUCTS[2]),
    "/api/orders", "/api/orders", price(PRODUCTS[3]), "/api/orders", "/",
    "/api/orders", "/api/orders", price(PRODUCTS[4]), "/api/orders", price(PRODUCTS[5]),
]


def path_at(second):
    """The path for this whole second since the epoch: the clock is the rotation's only state."""
    return ROTATION[int(second) % len(ROTATION)]


def fetch(url, timeout):
    """The status a GET answers with: 0 when nothing answered in time."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        return 0


def traffic_on(value, now):
    """"off", unset, unreadable and past all mean off."""
    try:
        until = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=datetime.timezone.utc)
    return until.timestamp() > now


def is_failure(status):
    return status == 0 or status >= 500


def summarize(results):
    """The minute's summary from [(path, status)]. The paths read as a person would write them."""
    by_status, failed_paths = {}, {}
    for path, status in results:
        by_status[str(status)] = by_status.get(str(status), 0) + 1
        if is_failure(status):
            shown = urllib.parse.unquote(path)
            failed_paths[shown] = failed_paths.get(shown, 0) + 1
    return {"requests": len(results), "failed": sum(failed_paths.values()), "by_status": by_status,
            "failed_paths": failed_paths}


def send_all(base, now=time.time, sleep=time.sleep, get=fetch, remaining=None):
    """60 requests, one on each whole second from the next one on, each on its own thread, so
    a slow answer can't delay the next. remaining() is what is left of the function's time."""
    first = int(now()) + 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        running = []
        for slot in range(first, first + PER_RUN):
            wait = slot - now()
            if wait > 0:
                sleep(wait)
            path = path_at(slot)
            left = remaining() - SUMMARY_MARGIN_SECONDS if remaining else REQUEST_TIMEOUT
            running.append((path, pool.submit(get, base + path, max(0.1, min(REQUEST_TIMEOUT, left)))))
        return [(path, future.result()) for path, future in running]


_loki = None
_ssm = None


def loki_settings(ssm):
    """(push URL, Basic auth), read once per container. A failed push forgets them, so a rotated token is found."""
    global _loki
    if _loki is None:
        value = lambda name: ssm.get_parameter(Name=f"{SSM_PREFIX}grafana/{name}", WithDecryption=True)["Parameter"]["Value"]
        auth = base64.b64encode(f"{value('logs-user')}:{value('logs-token')}".encode()).decode()
        _loki = (value("logs-url").rstrip("/") + "/loki/api/v1/push", auth)
    return _loki


def push(ssm, summary, timeout=3.0):
    """One line to Grafana Cloud Logs. A summary that can't be sent stops nothing: the next run has its own."""
    global _loki
    try:
        url, auth = loki_settings(ssm)
        body = {"streams": [{"stream": LABELS, "values": [[str(time.time_ns()), json.dumps(summary)]]}]}
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Basic {auth}", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout, context=TLS) as response:
            return response.status
    except Exception as error:
        _loki = None
        print(f"Summary not sent to Grafana ({error.__class__.__name__}).")
        return 0


def client():
    """One SSM client per container: at 128 MB, making it again every minute would be most of an idle day's cost."""
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def handler(event, context, ssm=None, now=time.time, sleep=time.sleep, get=fetch):
    ssm = ssm or client()
    try:
        value = ssm.get_parameter(Name=SSM_PREFIX + SWITCH)["Parameter"]["Value"]
    except Exception as error:  # no name yet (infra/ not applied) or no right to read it: off, and say why
        print(f"Switch not read ({error.__class__.__name__}): no traffic.")
        return {"sent": 0}
    if not traffic_on(value, now()):
        return {"sent": 0}
    remaining = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None
    results = send_all(os.environ["TARGET_URL"].rstrip("/"), now=now, sleep=sleep, get=get, remaining=remaining)
    summary = summarize(results)
    print(json.dumps(summary))
    left = remaining() if remaining else 3.0
    push(ssm, summary, timeout=max(0.1, min(3.0, left - 0.2)))
    return {"sent": summary["requests"], "failed": summary["failed"]}
