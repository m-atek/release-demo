"""orders-service: the app the release demo ships.

An AWS Lambda function behind a function URL. It serves one page with the
service, environment, version, commit and deploy time in large type, a /health
endpoint for the pipeline's checks, and /api/orders with today's fictional
orders for the desktop and mobile demos. Standard library only.
"""

import datetime
import hashlib
import html
import itertools
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = "orders-service"

STORES = [
    "Utrecht Centraal",
    "Amsterdam Zuid",
    "Rotterdam Blaak",
    "Eindhoven Strijp",
    "Den Haag Spui",
    "Groningen Vismarkt",
]
PRODUCTS = [
    ("Running shoes", 89.95),
    ("Rain jacket", 74.50),
    ("Day backpack", 49.00),
    ("Water bottle", 12.95),
    ("Training shirt", 24.99),
    ("Sports socks, 3 pairs", 14.99),
]
STATUSES = ["received", "picking", "packed", "shipped"]
DELIVERY_ESTIMATE_DAYS = 2

_deployed_at = None


def read_package(directory):
    """The version, and the commit the build step wrote, or "local" before a build."""
    version = (directory / "VERSION").read_text().strip()
    info = directory / "build_info.json"
    commit = json.loads(info.read_text()).get("commit", "unknown") if info.exists() else "local"
    return version, commit


# Read once at cold start: a published Lambda version never changes.
VERSION, COMMIT = read_package(HERE)


def environment():
    return os.environ.get("ENVIRONMENT", "local")


def deployed_at():
    """When this version was published: the Lambda version's LastModified."""
    global _deployed_at
    if _deployed_at is None:
        _deployed_at = os.environ.get("DEPLOYED_AT") or _lambda_last_modified()
    return _deployed_at


def _lambda_last_modified():
    name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not name:
        return "unknown"
    try:
        import boto3

        config = boto3.client("lambda").get_function_configuration(
            FunctionName=name,
            Qualifier=os.environ.get("AWS_LAMBDA_FUNCTION_VERSION", "$LATEST"),
        )
        modified = datetime.datetime.strptime(config["LastModified"], "%Y-%m-%dT%H:%M:%S.%f%z")
        return modified.strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        return "unknown"


def todays_orders(today=None):
    """Fictional orders, the same all day, different every day."""
    today = today or datetime.date.today()
    pick = _picker(today.isoformat())
    orders = []
    for number in range(1, 8 + pick(7) + 1):
        first = pick(len(PRODUCTS))
        lines = [PRODUCTS[(first + n) % len(PRODUCTS)] for n in range(1 + pick(3))]
        orders.append(
            {
                "id": f"{today:%y%m%d}-{number:03d}",
                "store": STORES[pick(len(STORES))],
                "items": [name for name, _ in lines],
                "total_eur": round(sum(price for _, price in lines), 2),
                "status": STATUSES[pick(len(STATUSES))],
            }
        )
    return {"date": today.isoformat(), "orders": orders}


def _picker(seed):
    """pick(n) gives a number below n, derived from the seed and a counter: the
    same seed always gives the same numbers. For demo data, not for security."""
    counter = itertools.count()

    def pick(n):
        digest = hashlib.sha256(f"{seed}/{next(counter)}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % n

    return pick


def health():
    return {
        "status": "ok",
        "service": SERVICE,
        "environment": environment(),
        "version": VERSION,
        "commit": COMMIT,
    }


def page():
    env = environment()
    orders = todays_orders()["orders"]
    rows = "\n".join(
        f"<tr><td>{html.escape(o['id'])}</td><td>{html.escape(o['store'])}</td>"
        f"<td>{len(o['items'])}</td><td>&euro; {o['total_eur']:.2f}</td>"
        f"<td>{html.escape(o['status'])}</td></tr>"
        for o in orders[:5]
    )
    return PAGE.format(
        service=SERVICE,
        env=html.escape(env),
        version=html.escape(VERSION),
        commit=html.escape(COMMIT[:7]),
        deployed=html.escape(deployed_at()),
        delivery_days=DELIVERY_ESTIMATE_DAYS,
        order_count=len(orders),
        rows=rows,
    )


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")

    if method != "GET":
        return _json(405, {"error": "method not allowed"})
    if path == "/health":
        return _json(200, health())
    if path == "/api/orders":
        return _json(200, todays_orders())
    if path == "/":
        return {
            "statusCode": 200,
            "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
            "body": page(),
        }
    return _json(404, {"error": "not found"})


def _json(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{service} ({env})</title>
<style>
  :root {{
    --ink: #14181f; --muted: #5b6472; --line: #e3e6eb; --paper: #ffffff; --wash: #f5f6f8;
    --staging: #b45309; --production: #047857; --local: #4b5563;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--wash); color: var(--ink);
    font: 18px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 48px 24px; }}
  header {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
  h1 {{ margin: 0; font-size: 40px; letter-spacing: -0.02em; }}
  .env {{ padding: 4px 14px; border-radius: 999px; color: #fff; font-weight: 600;
    font-size: 20px; text-transform: uppercase; letter-spacing: 0.06em; }}
  .env.staging {{ background: var(--staging); }}
  .env.production {{ background: var(--production); }}
  .env.local {{ background: var(--local); }}
  .demo {{ margin-left: auto; color: var(--muted); font-size: 15px; }}
  .version {{ font-size: 120px; font-weight: 700; letter-spacing: -0.04em; line-height: 1;
    margin: 40px 0 8px; font-variant-numeric: tabular-nums; }}
  .facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px; margin: 32px 0; }}
  .fact {{ background: var(--paper); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; }}
  .fact span {{ display: block; color: var(--muted); font-size: 15px; }}
  .fact strong {{ font-size: 28px; font-variant-numeric: tabular-nums; }}
  .note {{ font-size: 30px; font-weight: 600; margin: 8px 0 0; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--paper);
    border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 16px; border-bottom: 1px solid var(--line); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 15px; }}
  tr:last-child td {{ border-bottom: 0; }}
  h2 {{ font-size: 22px; margin: 40px 0 12px; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>{service}</h1>
    <span class="env {env}">{env}</span>
    <span class="demo">Demo system</span>
  </header>
  <div class="version">{version}</div>
  <p class="note">Delivery estimate: {delivery_days} days</p>
  <section class="facts">
    <div class="fact"><span>Environment</span><strong>{env}</strong></div>
    <div class="fact"><span>Commit</span><strong>{commit}</strong></div>
    <div class="fact"><span>Deployed</span><strong>{deployed}</strong></div>
  </section>
  <h2>Today's orders ({order_count})</h2>
  <table>
    <thead><tr><th>Order</th><th>Store</th><th>Items</th><th>Total</th><th>Status</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</main>
</body>
</html>
"""
