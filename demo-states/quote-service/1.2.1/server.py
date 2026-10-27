"""quote-service: price quotes for the stores' online baskets.

One container image that runs unchanged in three clouds: AWS Lambda (through
the Lambda Web Adapter), Azure Container Apps and Google Cloud Run. It serves
one page with the service, cloud, environment and version in large type, a
/health endpoint for the pipeline's checks and the prober, and /api/quote.
Standard library only.

    GET /                                      the page
    GET /health                                status, version, cloud, environment
    GET /api/quote?amount=120.00&currency=GBP  one quote

Settings come from the environment: SERVICE_CLOUD (aws, azure or gcp),
SERVICE_ENVIRONMENT, SERVICE_REGION and PORT, and the GBP rate from
QUOTE_GBP_RATE.
"""

import datetime
import html
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = "quote-service"
CLOUDS = {
    "aws": ("AWS", "AWS Lambda"),
    "azure": ("Azure", "Azure Container Apps"),
    "gcp": ("Google Cloud", "Google Cloud Run"),
    "local": ("Local", "this computer"),
}
# EUR is the stores' own currency. ORD-60: quotes in GBP too, for the UK stores,
# at the rate the stores set.
CURRENCIES = ("EUR", "GBP")
QUOTE_DAYS = 30
EXAMPLE_BASKET_EUR = 120.00


def read_package(directory):
    """The version, and the commit the build step wrote, or "local" before a build."""
    version = (directory / "VERSION").read_text().strip()
    info = directory / "build_info.json"
    commit = json.loads(info.read_text()).get("commit", "unknown") if info.exists() else "local"
    return version, commit


# Read once at start: an image never changes.
VERSION, COMMIT = read_package(HERE)


def cloud():
    key = os.environ.get("SERVICE_CLOUD", "local")
    return key if key in CLOUDS else "local"


def environment():
    return os.environ.get("SERVICE_ENVIRONMENT", "local")


def health():
    return {
        "status": "ok",
        "service": SERVICE,
        "version": VERSION,
        "cloud": cloud(),
        "environment": environment(),
        "commit": COMMIT,
    }


class QuoteError(Exception):
    """A quote that can't be made: the message says why, the status how bad."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def rate(currency):
    """How many units of the currency one euro buys."""
    if currency == "EUR":
        return 1.0
    if currency == "GBP":
        return gbp_rate()
    raise QuoteError(400, f"No quotes in {currency}. Quotes come in {' and '.join(CURRENCIES)}.")


def gbp_rate():
    setting = os.environ.get("QUOTE_GBP_RATE", "").strip()
    if not setting:
        raise QuoteError(500, "QUOTE_GBP_RATE is not set")
    try:
        return float(setting)
    except ValueError:
        raise QuoteError(500, f"QUOTE_GBP_RATE is not a number: {setting!r}")


def quote(amount_eur, currency, today=None):
    if amount_eur <= 0:
        raise QuoteError(400, "The amount must be more than zero.")
    today = today or datetime.date.today()
    per_eur = rate(currency)
    return {
        "amount_eur": round(amount_eur, 2),
        "currency": currency,
        "rate": per_eur,
        "total": round(amount_eur * per_eur, 2),
        "valid_until": (today + datetime.timedelta(days=QUOTE_DAYS)).isoformat(),
    }


def quote_from_query(query):
    params = urllib.parse.parse_qs(query)
    try:
        amount = float(params.get("amount", ["0"])[0])
    except ValueError:
        raise QuoteError(400, "The amount must be a number, such as 120.00.")
    return quote(amount, params.get("currency", ["EUR"])[0].upper())


def page():
    cloud_name, platform = CLOUDS[cloud()]
    rows = "\n".join(
        f"<tr><td>{c}</td><td>{q['rate']:.2f}</td><td>{q['total']:,.2f} {c}</td></tr>"
        for c, q in ((c, quote(EXAMPLE_BASKET_EUR, c)) for c in CURRENCIES)
    )
    region = os.environ.get("SERVICE_REGION", "")
    return PAGE.format(
        service=SERVICE,
        cloud=cloud(),
        cloud_name=html.escape(cloud_name),
        env=html.escape(environment()),
        version=html.escape(VERSION),
        commit=html.escape(COMMIT[:7]),
        runs_on=html.escape(f"{platform}, {region}" if region else platform),
        currencies=" and ".join(CURRENCIES),
        basket=f"{EXAMPLE_BASKET_EUR:,.2f}",
        rows=rows,
    )


def respond(method, target):
    """(status, content type, body) for one request."""
    path, _, query = target.partition("?")
    if method != "GET":
        return 405, "application/json", json.dumps({"error": "method not allowed"})
    try:
        if path == "/health":
            return 200, "application/json", json.dumps(health())
        if path == "/api/quote":
            return 200, "application/json", json.dumps(quote_from_query(query))
        if path == "/":
            return 200, "text/html; charset=utf-8", page()
    except QuoteError as error:
        return error.status, "application/json", json.dumps({"error": str(error)})
    return 404, "application/json", json.dumps({"error": "not found"})


class Handler(BaseHTTPRequestHandler):
    server_version = SERVICE

    def do_GET(self):
        self.answer("GET")

    def do_POST(self):
        self.answer("POST")

    def answer(self, method):
        status, content_type, body = respond(method, self.path)
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        # One line per request on stdout: each cloud collects it as the app's log.
        print(f"{self.address_string()} {format % args}", file=sys.stdout, flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"{SERVICE} {VERSION} on {cloud()} ({environment()}), port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{service} ({cloud_name}, {env})</title>
<style>
  :root {{
    --ink: #14181f; --muted: #5b6472; --line: #e3e6eb; --paper: #ffffff; --wash: #f5f6f8;
    --staging: #b45309; --production: #047857; --local: #4b5563;
    --aws: #232f3e; --azure: #0063b1; --gcp: #1a73e8;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--wash); color: var(--ink);
    font: 18px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 48px 24px; }}
  header {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  h1 {{ margin: 0 4px 0 0; font-size: 40px; letter-spacing: -0.02em; }}
  .tag {{ padding: 4px 14px; border-radius: 999px; color: #fff; font-weight: 600;
    font-size: 20px; text-transform: uppercase; letter-spacing: 0.06em; background: var(--local); }}
  .tag.staging {{ background: var(--staging); }}
  .tag.production {{ background: var(--production); }}
  .tag.aws {{ background: var(--aws); }}
  .tag.azure {{ background: var(--azure); }}
  .tag.gcp {{ background: var(--gcp); }}
  .demo {{ margin-left: auto; color: var(--muted); font-size: 15px; }}
  .version {{ font-size: 120px; font-weight: 700; letter-spacing: -0.04em; line-height: 1;
    margin: 40px 0 8px; font-variant-numeric: tabular-nums; }}
  .note {{ font-size: 30px; font-weight: 600; margin: 8px 0 0; }}
  .facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px; margin: 32px 0; }}
  .fact {{ background: var(--paper); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; }}
  .fact span {{ display: block; color: var(--muted); font-size: 15px; }}
  .fact strong {{ font-size: 24px; font-variant-numeric: tabular-nums; }}
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
    <span class="tag {cloud}">{cloud_name}</span>
    <span class="tag {env}">{env}</span>
    <span class="demo">Demo system</span>
  </header>
  <div class="version">{version}</div>
  <p class="note">Quotes in {currencies}</p>
  <section class="facts">
    <div class="fact"><span>Runs on</span><strong>{runs_on}</strong></div>
    <div class="fact"><span>Environment</span><strong>{env}</strong></div>
    <div class="fact"><span>Commit</span><strong>{commit}</strong></div>
  </section>
  <h2>A basket of {basket} EUR</h2>
  <table>
    <thead><tr><th>Currency</th><th>Rate</th><th>Quote</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</main>
</body>
</html>
"""


if __name__ == "__main__":
    main()
