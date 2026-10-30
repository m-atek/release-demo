"""Create the coding agent's own GitHub App, in one click.

    python scripts/demos/agent_ship/agent_app.py --owner m-atek

The same one-click manifest page as scripts/github_app.py, on
http://localhost:8765: you click once on GitHub to create the app, GitHub
sends you back here with a one-time code, and the script stores the app's
id, slug and private key in SSM (github/agent-app-id, github/agent-app-slug,
github/agent-app-private-key). Nothing is written to disk and the key is
never shown. Then install it on release-demo only, from the link it prints.

The app is release-demo-coding-agent-<owner>: contents and pull requests
write, metadata read, nothing else, and no webhook. It is in no ruleset's
bypass list (scripts/demos/agent_ship/ruleset.py).
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import agent
from common import fail, put_parameter, ssm_client

PORT = 8765
REPOSITORY = "release-demo"


def manifest(owner):
    return {
        "name": agent.app_name(owner),
        "url": f"https://github.com/{owner}/{REPOSITORY}",
        "description": "The coding agent of the release-demo platform: it may push its own branches and open pull "
                       "requests, and nothing else.",
        "redirect_url": f"http://localhost:{PORT}/callback",
        "public": False,
        "hook_attributes": {"url": "https://example.invalid/no-webhook", "active": False},
        "default_permissions": dict(agent.PERMISSIONS),
        "default_events": [],
    }


def convert(code):
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions", method="POST",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        fail(f"GitHub refused the code ({error.code}). Run the script again and click once more.")


def form(owner, state):
    return ('<!doctype html><meta charset="utf-8"><title>Create the coding agent\'s GitHub App</title>'
            '<body style="font:18px system-ui;max-width:640px;margin:64px auto">'
            f"<h1>The coding agent's GitHub App for {html.escape(owner)}/{REPOSITORY}</h1>"
            "<p>Contents and pull requests read and write, metadata read. Nothing else: no checks, statuses, "
            "workflows or administration, and no webhook.</p>"
            f'<form action="https://github.com/settings/apps/new?state={state}" method="post">'
            f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest(owner)))}">'
            '<button style="font:inherit;padding:10px 20px">Create it on GitHub</button></form></body>')


def store(ssm, result):
    put_parameter(ssm, "github/agent-app-id", str(result["id"]), secure=False)
    put_parameter(ssm, "github/agent-app-slug", result["slug"], secure=False)
    put_parameter(ssm, "github/agent-app-private-key", result["pem"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--owner", required=True, help="your GitHub username, which owns release-demo")
    args = parser.parse_args()

    ssm = ssm_client()
    state = os.urandom(16).hex()
    page = form(args.owner, state)
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
            self.reply(200, page)

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
    store(ssm, result)
    print(f"Created the app {result['slug']} and stored its id, slug and private key in SSM.")
    print(f"Now install it on {args.owner}/{REPOSITORY} only (Only select repositories):")
    print(f"  {result['html_url']}/installations/new")
    print(f"Then send back: the app's slug ({result['slug']}), which people.yaml's coding-agent entry names.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
