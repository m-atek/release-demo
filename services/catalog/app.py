"""catalog: the service the Kubernetes demo ships.

A small HTTP service in a container. It serves one page with the service,
environment, version and pod in large type, /health for the pipeline's checks,
/ready for the probes, and /api/products with the fictional product catalog,
searchable by name and by supplier.

The catalog comes from Postgres, named by CATALOG_POSTGRES_URL, through pg8000,
which the build puts in /app/vendor from requirements.txt. Each query opens a
new connection. While the database doesn't answer, the routes that need it
answer 503 and serve no data; /health doesn't touch it.
"""

import html
import json
import os
import signal
import socket
import sys
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE / "vendor"))
SERVICE = "catalog"
QUIET = {"/health", "/ready"}  # the probes would fill the log
DATABASE_TIMEOUT_SECONDS = 0.8  # under the probes' 1 s


def read_package(directory):
    """The version, and the commit the build step wrote, or "local" before a build."""
    version = (directory / "VERSION").read_text().strip()
    info = directory / "build_info.json"
    commit = json.loads(info.read_text()).get("commit", "unknown") if info.exists() else "local"
    return version, commit


VERSION, COMMIT = read_package(HERE)


def environment():
    return os.environ.get("ENVIRONMENT", "local")


def pod():
    return os.environ.get("POD_NAME") or socket.gethostname()


class DatabaseUnavailable(Exception):
    pass


def connect():
    """A new connection to the catalog database."""
    import pg8000.native

    parts = urlparse(os.environ["CATALOG_POSTGRES_URL"])
    return pg8000.native.Connection(
        user=parts.username, password=os.environ["CATALOG_POSTGRES_PASSWORD"], host=parts.hostname,
        port=parts.port or 5432, database=parts.path.lstrip("/"), timeout=DATABASE_TIMEOUT_SECONDS)


def run_query(sql):
    """The rows of one query, on a connection of its own."""
    try:
        connection = connect()
        try:
            return connection.run(sql)
        finally:
            connection.close()
    except Exception as error:  # whatever the driver or the network raises: no answer
        print(f"The catalog database didn't answer: {error.__class__.__name__}", flush=True)
        raise DatabaseUnavailable from error


def ask_database():
    """One query on a new connection: does the catalog database answer?"""
    try:
        run_query("SELECT count(*) FROM products")
    except DatabaseUnavailable:
        return False
    return True


def products():
    rows = run_query("SELECT id, name, supplier, price_eur FROM products ORDER BY id")
    return [{"id": id_, "name": name, "supplier": supplier, "price_eur": float(price)}
            for id_, name, supplier, price in rows]


def suppliers(items):
    return sorted({p["supplier"] for p in items})


def folded(text):
    """Lower case without accents, so "creme" finds "Crème"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def search(query="", supplier=""):
    """Products whose name holds query, from the supplier whose name holds supplier (ORD-90)."""
    found = [p for p in products() if folded(query) in folded(p["name"]) and folded(supplier) in folded(p["supplier"])]
    return {"query": query, "supplier": supplier, "count": len(found), "products": found}


def health():
    return {"status": "ok", "service": SERVICE, "environment": environment(), "version": VERSION,
            "commit": COMMIT, "pod": pod()}


def ready():
    """Ready while the catalog database answers."""
    return ask_database()


def page():
    items = products()
    rows = "\n".join(f"<tr><td>{html.escape(p['id'])}</td><td>{html.escape(p['name'])}</td>"
                     f"<td>{html.escape(p['supplier'])}</td><td>&euro; {p['price_eur']:.2f}</td></tr>"
                     for p in items[:6])
    return PAGE.format(service=SERVICE, env=html.escape(environment()), version=html.escape(VERSION),
                       commit=html.escape(COMMIT[:7]), pod=html.escape(pod()), count=len(items), rows=rows,
                       suppliers=len(suppliers(items)))


def respond(target):
    """(status, content type, body) for a GET of target, a path with its query."""
    url = urlparse(target)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    if url.path == "/health":
        return 200, "application/json", json.dumps(health())
    if url.path == "/ready":
        ok = ready()
        return (200 if ok else 503), "application/json", json.dumps({"ready": ok, "version": VERSION})
    try:
        if url.path == "/api/products":
            return 200, "application/json", json.dumps(search(query.get("q", ""), query.get("supplier", "")))
        if url.path == "/api/suppliers":
            return 200, "application/json", json.dumps({"suppliers": suppliers(products())})
        if url.path == "/":
            return 200, "text/html; charset=utf-8", page()
    except DatabaseUnavailable:
        return 503, "application/json", json.dumps({"error": "the catalog database is unavailable"})
    return 404, "application/json", json.dumps({"error": "not found"})


class Handler(BaseHTTPRequestHandler):
    server_version = "catalog"
    sys_version = ""

    def do_GET(self):
        status, content_type, body = respond(self.path)
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        if urlparse(self.path).path not in QUIET:
            sys.stderr.write(f"{self.address_string()} {format % args}\n")


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    # Kubernetes sends SIGTERM once the pod is out of the Service: finish what is in flight, then stop.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"{SERVICE} {VERSION} in {environment()} on :{port}, {'ready' if ready() else 'not ready'}", flush=True)
    server.serve_forever()


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
    font-size: 20px; text-transform: uppercase; letter-spacing: 0.06em; background: var(--local); }}
  .env.staging {{ background: var(--staging); }}
  .env.production {{ background: var(--production); }}
  .demo {{ margin-left: auto; color: var(--muted); font-size: 15px; }}
  .version {{ font-size: 120px; font-weight: 700; letter-spacing: -0.04em; line-height: 1;
    margin: 40px 0 8px; font-variant-numeric: tabular-nums; }}
  .facts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px; margin: 32px 0; }}
  .fact {{ background: var(--paper); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; }}
  .fact span {{ display: block; color: var(--muted); font-size: 15px; }}
  .fact strong {{ font-size: 24px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--paper);
    border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 16px; border-bottom: 1px solid var(--line); }}
  th {{ color: var(--muted); font-weight: 500; font-size: 15px; }}
  tr:last-child td {{ border-bottom: 0; }}
  h2 {{ font-size: 22px; margin: 40px 0 12px; }}
  .note {{ font-size: 30px; font-weight: 600; margin: 8px 0 0; }}
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
  <p class="note">Search by supplier: {suppliers} suppliers</p>
  <section class="facts">
    <div class="fact"><span>Environment</span><strong>{env}</strong></div>
    <div class="fact"><span>Commit</span><strong>{commit}</strong></div>
    <div class="fact"><span>Pod</span><strong>{pod}</strong></div>
  </section>
  <h2>Products ({count})</h2>
  <table>
    <thead><tr><th>Product</th><th>Name</th><th>Supplier</th><th>Price</th></tr></thead>
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
