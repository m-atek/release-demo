"""Create the GitHub App the pipeline pushes and tags with, in one click.

    python scripts/github_app.py --owner <your GitHub username>

Serves a page on http://localhost:8765 with GitHub's app manifest form. You
click once on GitHub to create the app; GitHub sends you back here, and the
script stores the app id and private key in SSM. The key is never shown or
written to disk. Then install the app on release-demo from the link it prints.

The app can read and write repository contents (stage commits, tags and
releases) and read metadata. It has no webhook: GitHub can't reach Jenkins.
"""

import argparse
import html
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from common import fail, put_parameter, ssm_client

PORT = 8765
REPOSITORY = "release-demo"


def manifest(owner):
    return {
        "name": f"release-demo-{owner}"[:34],
        "url": f"https://github.com/{owner}/{REPOSITORY}",
        "description": "Pushes demo commits, tags and releases for the release-demo pipeline.",
        "redirect_url": f"http://localhost:{PORT}/callback",
        "public": False,
        "hook_attributes": {"url": "https://example.invalid/no-webhook", "active": False},
        "default_permissions": {"contents": "write", "metadata": "read", "checks": "write",
                                "pull_requests": "write", "statuses": "write"},
        "default_events": [],
    }


def convert(code):
    """Exchange the one-time code for the app's id, slug and private key."""
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions", method="POST",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        fail(f"GitHub refused the code ({error.code}). Run the script again and click once more.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", required=True, help="your GitHub username, which owns release-demo")
    args = parser.parse_args()

    ssm = ssm_client()
    state = os.urandom(16).hex()
    form = (
        '<!doctype html><meta charset="utf-8"><title>Create the GitHub App</title>'
        '<body style="font:18px system-ui;max-width:640px;margin:64px auto">'
        f"<h1>GitHub App for {html.escape(args.owner)}/{REPOSITORY}</h1>"
        "<p>Contents, checks, pull requests and commit statuses read and write, metadata read, no webhook.</p>"
        f'<form action="https://github.com/settings/apps/new?state={state}" method="post">'
        f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest(args.owner)))}">'
        '<button style="font:inherit;padding:10px 20px">Create it on GitHub</button></form></body>'
    )
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/callback":
                if query.get("state", [""])[0] != state or "code" not in query:
                    return self.reply(400, "<p>That link isn't from this run. Start the script again.</p>")
                result.update(convert(query["code"][0]))
                return self.reply(200, "<p>Done. Go back to the terminal.</p>")
            self.reply(200, form)

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Open http://localhost:{PORT} and click 'Create it on GitHub'.")
    while not result:
        server.handle_request()
    server.server_close()

    put_parameter(ssm, "github/app-id", str(result["id"]), secure=False)
    put_parameter(ssm, "github/app-private-key", result["pem"])
    put_parameter(ssm, "github/repository", f"{args.owner}/{REPOSITORY}", secure=False)
    print(f"Created the app {result['slug']} and stored its id and private key in SSM.")
    print(f"Now install it on {args.owner}/{REPOSITORY} only:")
    print(f"  {result['html_url']}/installations/new")


if __name__ == "__main__":
    main()
