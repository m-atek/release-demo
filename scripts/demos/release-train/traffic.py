"""Traffic for demo 21's switch take: orders-service production's /api/estimate, store by store.

    python scripts/demos/release-train/traffic.py                 about 2 requests a second, up to 30 minutes
    python scripts/demos/release-train/traffic.py --minutes 5 --rate 2

scripts/demo.sh traffic release-train starts it, 3 minutes before the switch,
so the errors panel shows a quiet baseline first. The requests go round the
six stores in turn, through the production function URL, as a store app's
would. Each result (store, status, latency) goes to Grafana Cloud Logs in
5-second batches, as demo 4's load generator sends its own (its Loki
client). With delivery_estimate_v2 off, every request answers 404; with it
on, one store in six, Groningen Vismarkt, answers 502 and Lambda counts the
error the alarm reads. It stops after --minutes or with Ctrl-C, and says
which stores failed.
"""

import argparse
import signal
import sys
import threading
import time
import urllib.parse
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
sys.path.insert(1, str(SCRIPTS))
sys.path.insert(2, str(SCRIPTS / "demos" / "apis"))

from common import http_get, service  # noqa: E402

SERVICE = "orders-service"
STORES = ["Utrecht Centraal", "Amsterdam Zuid", "Rotterdam Blaak", "Eindhoven Strijp", "Den Haag Spui",
          "Groningen Vismarkt"]
BATCH_SECONDS = 5


def production_url():
    import deploy

    client = deploy.lambda_client(SERVICE)
    function = service(SERVICE)["environments"]["production"]["function"]
    return client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")


def loki():
    try:
        from grafana import Loki  # demo 4's

        return Loki()
    except SystemExit:
        print("Grafana isn't set up: running without it.", file=sys.stderr)
        return None


def run(url, minutes, rate, sink, out=print):
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    batch, results = [], Counter()
    labels = (("source", "release-demo-loadgen"), ("service", SERVICE), ("environment", "production"))
    out(f"traffic: {rate} requests a second to /api/estimate, the six stores in turn, for up to {minutes} minutes")
    deadline = time.monotonic() + minutes * 60
    next_at, last_push, index = time.monotonic(), time.monotonic(), 0
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            store = STORES[index % len(STORES)]
            index += 1
            started = time.perf_counter()
            status, _ = http_get(f"{url}/api/estimate?store={urllib.parse.quote(store)}", timeout=10)
            took = (time.perf_counter() - started) * 1000
            results[(store, status)] += 1
            batch.append((time.time_ns(), {"store": store, "status": status, "failed": int(status >= 500 or status == 0),
                                          "latency_ms": round(took, 1), "path": "/api/estimate"}))
            if time.monotonic() - last_push >= BATCH_SECONDS:
                if sink:
                    sink.push({labels: batch})
                batch, last_push = [], time.monotonic()
            next_at += 1 / rate
            stop.wait(max(0.0, next_at - time.monotonic()))
    except KeyboardInterrupt:
        pass
    if sink and batch:
        sink.push({labels: batch})
    failed = sorted({store for (store, status), count in results.items() if status >= 500 or status == 0})
    out("traffic done: " + ", ".join(f"{store} {status}: {count}" for (store, status), count in sorted(results.items())))
    out(f"failed stores: {', '.join(failed) or 'none'}")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--rate", type=float, default=2)
    parser.add_argument("--no-grafana", action="store_true")
    args = parser.parse_args()
    run(production_url(), min(args.minutes, 30), args.rate, None if args.no_grafana else loki(),
        out=lambda text: print(text, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
