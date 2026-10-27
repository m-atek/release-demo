"""Traffic for pricing-api's canary: about 10 requests a second, a third of
them for Belgian stores, each result pushed to Grafana Cloud Logs.

    python scripts/demos/apis/loadgen.py --seconds 360 --versions 2.0.4,2.1.1
    python scripts/demos/apis/loadgen.py --environment staging --seconds 30 --rate 5

The production check starts it on the tools host when a canary begins and
stops it after the decision, so it runs during each canary and only then. It
also stops by itself after --seconds, so a run that dies can't leave it on.

It calls the alias live with the same event API Gateway sends, because only
Lambda's own answer says which version handled a call that failed: API
Gateway turns a raised error into a bare 500. Each result (version, status,
latency) goes to Loki in 5-second batches, which is what the Grafana panels
show within seconds. CloudWatch's one-minute metrics decide; these only show.
With --versions, each batch also names those versions in a stream of its own,
so the Request share panel draws a version at 0% before its canary starts
and after it is pulled back, not a line that begins or ends in mid-air.
"""

import argparse
import json
import queue
import random
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pricing
from grafana import Loki

NL_STORES = ["NL-UTR-01", "NL-AMS-02", "NL-RTM-03", "NL-EHV-04"]
BE_STORES = ["BE-ANT-01", "BE-BRU-02", "BE-GNT-03"]
BATCH_SECONDS = 5
SUMMARY_EVERY = 6  # batches: one log line every 30 seconds


def api_event(path, store):
    """An API Gateway HTTP API event, payload format 2.0, for a GET."""
    return {
        "version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": f"store={store}",
        "queryStringParameters": {"store": store},
        "headers": {"user-agent": "release-demo-loadgen", "accept": "application/json"},
        "requestContext": {"http": {"method": "GET", "path": path, "protocol": "HTTP/1.1",
                                    "sourceIp": "127.0.0.1", "userAgent": "release-demo-loadgen"},
                           "stage": "$default"},
        "isBase64Encoded": False,
    }


def call(client, function, path, store):
    """One call through the alias live. Returns (status, Lambda version, app version or None, error, ms)."""
    started = time.perf_counter()
    try:
        reply = client.invoke(FunctionName=function, Qualifier="live", Payload=json.dumps(api_event(path, store)))
    except client.exceptions.TooManyRequestsException:
        return 429, None, None, "Throttled", (time.perf_counter() - started) * 1000
    payload = json.loads(reply["Payload"].read() or b"{}")
    took = (time.perf_counter() - started) * 1000
    if reply.get("FunctionError"):
        return 500, reply["ExecutedVersion"], None, payload.get("errorType", reply["FunctionError"]), took
    headers = payload.get("headers", {})
    return payload.get("statusCode", 502), reply["ExecutedVersion"], headers.get("x-pricing-api-version"), "", took


def percentile(values, share):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


class Totals:
    """Per version: requests, failures and latencies, for the log and the final summary."""

    def __init__(self):
        self.requests, self.failed, self.latency = defaultdict(int), defaultdict(int), defaultdict(list)

    def add(self, line):
        self.requests[line["version"]] += 1
        self.failed[line["version"]] += line["failed"]
        self.latency[line["version"]].append(line["latency_ms"])

    def lines(self):
        for version in sorted(self.requests):
            count, failed = self.requests[version], self.failed[version]
            yield (f"{version}: {count} requests, {failed} failed ({100 * failed / count:.1f}%), "
                   f"p95 {percentile(self.latency[version], 0.95):.0f} ms")


def run(environment, seconds, rate, belgian, loki, versions=(), out=print):
    function = pricing.environment(environment)["function"]
    from botocore.config import Config

    client = pricing.deploy_session().client("lambda", config=Config(
        retries={"max_attempts": 1, "mode": "standard"}, max_pool_connections=16, read_timeout=10))
    skus = [p["sku"] for p in json.loads(json.loads(client.invoke(
        FunctionName=function, Qualifier="live",
        Payload=json.dumps(api_event("/products", NL_STORES[0])))["Payload"].read())["body"])["products"]]
    pick = random.Random()  # which store and product: for load, not for security
    results = queue.Queue()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    labels = lambda version, source="release-demo-loadgen": (
        ("source", source), ("service", pricing.SERVICE), ("environment", environment), ("version", version))

    def one():
        region = "BE" if pick.random() < belgian else "NL"
        store = pick.choice(BE_STORES if region == "BE" else NL_STORES)
        sku = pick.choice(skus)
        at = time.time_ns()
        status, number, app, error, took = call(client, function, f"/prices/{sku}", store)
        app = app or (pricing.app_version(client, function, number) if number else "none")
        results.put((at, {"version": app, "lambda_version": number, "status": status, "failed": int(status >= 500),
                          "latency_ms": round(took, 1), "store": store, "region": region, "sku": sku, "error": error}))

    total = Totals()
    recent = [Totals()]  # since the last log line

    def flush():
        streams = defaultdict(list)
        for version in versions:
            streams[labels(version, "release-demo-loadgen-versions")].append((time.time_ns(), {"version": version}))
        while not results.empty():
            at, line = results.get()
            streams[labels(line["version"])].append((at, line))
            total.add(line)
            recent[0].add(line)
        if loki:
            loki.push(streams)

    def flusher():
        batches = 0
        while not stop.wait(BATCH_SECONDS):
            flush()
            batches += 1
            if batches % SUMMARY_EVERY == 0:
                out(f"load {time.strftime('%H:%M:%S', time.gmtime())} UTC  " + " · ".join(recent[0].lines()))
                recent[0] = Totals()

    out(f"load: {rate} requests a second to {function}:live for up to {seconds}s, "
        f"{belgian:.0%} for Belgian stores, results to Grafana every {BATCH_SECONDS}s"
        + ("" if loki else " (Grafana off)"))
    threading.Thread(target=flusher, daemon=True).start()
    deadline = time.monotonic() + seconds
    next_at = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        while not stop.is_set() and time.monotonic() < deadline:
            pool.submit(one)
            next_at += 1 / rate
            stop.wait(max(0.0, next_at - time.monotonic()))
    stop.set()
    flush()
    out("load done: " + (" · ".join(total.lines()) or "no requests"))
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", default="production", choices=["staging", "production"])
    parser.add_argument("--seconds", type=int, default=360, help="stop after this long at the latest")
    parser.add_argument("--rate", type=float, default=None, help="requests a second (services.yaml)")
    parser.add_argument("--belgian", type=float, default=None, help="share for Belgian stores (services.yaml)")
    parser.add_argument("--versions", default="", help="versions to show in Grafana even at 0%%, such as 2.0.4,2.1.1")
    parser.add_argument("--no-grafana", action="store_true")
    args = parser.parse_args()

    load = pricing.config()["canary"]["load"]
    loki = None
    if not args.no_grafana:
        try:
            loki = Loki()
        except SystemExit:
            print("Grafana isn't set up: running without it.", file=sys.stderr)
    run(args.environment, args.seconds, args.rate or load["requests_per_second"],
        args.belgian if args.belgian is not None else load["belgian_share"], loki,
        versions=[v for v in args.versions.split(",") if v], out=lambda text: print(text, flush=True))


if __name__ == "__main__":
    sys.exit(main())
