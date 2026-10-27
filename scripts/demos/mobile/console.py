"""The stand-ins' state as three pages a camera can show, on http://127.0.0.1:8077.

    python scripts/demos/mobile/console.py            the demo's bucket, with your AWS sign-in
    MOBILE_STANDIN_DIR=.local/standins python scripts/demos/mobile/console.py

/jamf      Device management: Store Floor's version, build and scope, the
           pilot devices' inventory, every store device, the last API calls.
/testflight  iOS testing: the builds, their state, group and What to Test.
/play      Android testing: the internal track's release, its version code
           and release notes, and the uploaded bundles.

With demo 6's shared Jamf stand-in (scripts/demos/standins/jamf.py), /jamf
opens that stand-in's own Device management page, which shows Store Floor's
scope and the inventory of its groups.

The pages refresh every three seconds. They read the stand-ins' state only;
the pipeline changes it. They carry no product names and no stand-in label:
the videos open with their own disclaimer, and this startup log says it.
"""

import argparse
import datetime
import html
import http.server
import os
import sys

import mobile

PAGES = {"/jamf": ("Device management", "jamf.json"), "/testflight": ("iOS testing", "testflight.json"),
         "/play": ("Android testing", "google-play.json")}

STYLE = """
:root { --ink: #14181f; --muted: #5b6472; --line: #e3e6eb; --paper: #fff; --wash: #f5f6f8; --accent: #047857; --warn: #b45309; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--wash); color: var(--ink); font: 18px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
nav { background: var(--ink); padding: 10px 24px; display: flex; gap: 24px; }
nav a { color: #cfd6df; text-decoration: none; font-weight: 600; } nav a.on { color: #fff; }
main { max-width: 1100px; margin: 0 auto; padding: 28px 24px 48px; }
h1 { margin: 0 0 18px; font-size: 30px; } h2 { font-size: 20px; margin: 28px 0 10px; color: var(--muted); }
.card { background: var(--paper); border: 1px solid var(--line); border-radius: 12px; padding: 20px 24px; }
.big { font-size: 44px; font-weight: 700; letter-spacing: -0.01em; }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 6px 24px; margin-top: 10px; }
.facts dt { color: var(--muted); } .facts dd { margin: 0; font-weight: 600; }
.pill { display: inline-block; padding: 2px 12px; border-radius: 999px; background: #e7f5ef; color: var(--accent); font-weight: 700; margin-right: 6px; }
.pill.warn { background: #fdf1e3; color: var(--warn); }
table { width: 100%; border-collapse: collapse; background: var(--paper); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: #eef0f3; font-size: 15px; color: var(--muted); }
pre { margin: 0; white-space: pre-wrap; font: inherit; }
code { font: 15px/1.4 ui-monospace, Menlo, monospace; word-break: break-all; }
.muted { color: var(--muted); }
"""


def when(seconds):
    return datetime.datetime.fromtimestamp(seconds).strftime("%d %b %H:%M:%S") if seconds else ""


def e(value):
    return html.escape(str(value if value is not None else ""))


def requests_table(state):
    rows = "".join(f"<tr><td class=muted>{e(when(r['at']))}</td><td><code>{e(r['request'])}</code></td>"
                   f"<td>{e(r['result'])}</td></tr>" for r in state.get("requests", [])[:12])
    return f"<h2>Last API calls</h2><table><tr><th>Time</th><th>Request</th><th>Result</th></tr>{rows}</table>"


def jamf_page(state):
    config = mobile.config()
    bundle, pilot, everyone = config["app"]["bundle_id"], config["jamf"]["pilot_group"], config["jamf"]["all_group"]
    app = next(iter(state["apps"].values()))
    scope = "".join(f"<span class=pill>{e(g)}</span>" for g in app["scope"])

    def rows(ids):
        out = ""
        for i in ids:
            device = state["devices"][str(i)]
            installed = device["apps"].get(bundle) or {}
            out += (f"<tr><td>{e(device['name'])}</td><td>{e(device['model'])}</td>"
                    f"<td><b>{e(installed.get('version'))}</b> ({e(installed.get('build'))})</td>"
                    f"<td>{e(device.get('last_result'))}</td><td class=muted>{e(when(device['last_inventory']))}</td></tr>")
        return out

    head = "<tr><th>Device</th><th>Model</th><th>Store Floor</th><th>Last install</th><th>Inventory update</th></tr>"
    counts = {}
    for i in state["groups"].get(everyone, []):
        version = (state["devices"][str(i)]["apps"].get(bundle) or {}).get("version")
        counts[version] = counts.get(version, 0) + 1
    summary = ", ".join(f"{n} on {e(v)}" for v, n in sorted(counts.items(), key=lambda kv: str(kv[0])))
    return (f"<h1>Mobile device apps</h1><div class=card id=app-{e(app['id'])}><div class=muted>In-house app</div>"
            f"<div class=big>{e(app['name'])} {e(app['version'])}</div><dl class=facts>"
            f"<dt>Build</dt><dd>{e(app['build'])}</dd><dt>Bundle identifier</dt><dd>{e(app['bundle_id'])}</dd>"
            f"<dt>Scope</dt><dd>{scope}</dd><dt>Automatic updates</dt><dd>{'On' if app.get('automatic_updates') else 'Off'}</dd>"
            f"<dt>Uploaded</dt><dd>{e(when(app.get('uploaded_at')))} {e((app.get('file') or {}).get('name'))}</dd></dl></div>"
            f"<h2>Inventory: {e(pilot)}</h2><table>{head}{rows(state['groups'].get(pilot, []))}</table>"
            f"<h2>Inventory: {e(everyone)} ({summary})</h2><table>{head}{rows(state['groups'].get(everyone, []))}</table>"
            + requests_table(state))


def testflight_page(state):
    rows = ""
    for build in state["builds"][:10]:
        status = build["internal_state"].replace("_", " ").title()
        pill = "pill warn" if build["expired"] or "MISSING" in build["internal_state"] else "pill"
        rows += (f"<tr><td><b>{e(build['version'])}</b> ({e(build['build'])})</td><td><span class='{pill}'>{e(status)}</span></td>"
                 f"<td>{e(', '.join(build['groups']) or '')}</td><td><pre>{e(build.get('whats_new') or '')}</pre></td>"
                 f"<td class=muted>{e(when(build['uploaded_at']))}</td></tr>")
    latest = state["builds"][0] if state["builds"] else None
    top = (f"<div class=card><div class=muted>Latest build</div><div class=big>{e(state['app']['name'])} "
           f"{e(latest['version'])} ({e(latest['build'])})</div><dl class=facts><dt>Processing</dt><dd>{e(latest['processing'])}</dd>"
           f"<dt>Internal testing</dt><dd>{e(', '.join(latest['groups']) or 'not yet')}</dd>"
           f"<dt>What to Test</dt><dd><pre>{e(latest.get('whats_new') or '')}</pre></dd></dl></div>" if latest else
           "<div class=card>No builds yet.</div>")
    return (f"<h1>Builds for testers</h1>{top}<h2>Builds</h2><table><tr><th>Version (build)</th><th>Status</th>"
            f"<th>Groups</th><th>What to Test</th><th>Uploaded</th></tr>{rows}</table>" + requests_table(state))


def play_page(state):
    tracks = ""
    for name, track in state["tracks"].items():
        for release in track.get("releases", []):
            notes = (release.get("releaseNotes") or [{}])[0].get("text", "")
            tracks += (f"<div class=card><div class=muted>{e(name.capitalize())} testing track</div>"
                       f"<div class=big>{e(release.get('name'))}</div><dl class=facts>"
                       f"<dt>Version code</dt><dd>{e(', '.join(release['versionCodes']))}</dd>"
                       f"<dt>Status</dt><dd><span class=pill>{e(release['status'].capitalize())}</span></dd>"
                       f"<dt>Release notes</dt><dd><pre>{e(notes)}</pre></dd>"
                       f"<dt>Updated</dt><dd>{e(when(release.get('updated_at')))}</dd></dl></div>")
    bundles = "".join(f"<tr><td><b>{e(b['version_code'])}</b></td><td>{e(b['version_name'])}</td>"
                      f"<td>API level {e(b['target_sdk'])}</td><td class=muted>{e(when(b['uploaded_at']))}</td></tr>"
                      for b in state["bundles"][:10])
    return (f"<h1>{e(state['app']['name'])} test releases</h1>{tracks or '<div class=card>No release yet.</div>'}"
            f"<h2>App bundles</h2><table><tr><th>Version code</th><th>Version</th><th>Target</th><th>Uploaded</th></tr>"
            f"{bundles}</table>" + requests_table(state))


RENDER = {"/jamf": jamf_page, "/testflight": testflight_page, "/play": play_page}


def page(path, store):
    title, key = PAGES[path]
    state = store.read(key)
    body = RENDER[path](state) if state else "<div class=card>Nothing here yet. Run reset.py --standins.</div>"
    links = "".join(f"<a href='{p}' class='{'on' if p == path else ''}'>{t}</a>" for p, (t, _) in PAGES.items())
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta http-equiv=refresh content=3>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'><title>{title}</title>"
            f"<style>{STYLE}</style></head><body><nav>{links}</nav><main>{body}</main></body></html>")


def summary(store):
    """Three lines for scripts/demo.sh status mobile."""
    config = mobile.config()
    bundle = config["app"]["bundle_id"]
    jamf, flight, play = (store.read(k) for k in ("jamf.json", "testflight.json", "google-play.json"))
    import jamf_apps

    client = jamf_apps.jamf(store=None) if jamf_apps.SHARED.exists() and not os.environ.get("MOBILE_STANDIN_DIR") else None
    if client is not None:
        app = client.find_app(config["jamf"]["app"])
        seen = ", ".join(f"{(client.installed(d['id'], bundle) or {}).get('version')}"
                         for d in client.devices(config["jamf"]["pilot_group"]))
        print(f"  device management: {app['name']} {app['version']} ({app['build']}), scope {', '.join(app['scope'])}; "
              f"pilot devices on {seen}")
    elif not jamf:
        print("  stand-ins: no state yet (scripts/demos/mobile/reset.py --standins)")
        return
    else:
        app = next(iter(jamf["apps"].values()))
        pilots = [jamf["devices"][str(i)] for i in jamf["groups"][config["jamf"]["pilot_group"]]]
        seen = ", ".join(f"{(d['apps'].get(bundle) or {}).get('version')} ({(d['apps'].get(bundle) or {}).get('build')})"
                         for d in pilots)
        print(f"  device management: {app['name']} {app['version']} ({app['build']}), scope {', '.join(app['scope'])}; "
              f"pilot devices on {seen}")
    latest = flight["builds"][0] if flight and flight["builds"] else None
    if latest:
        print(f"  iOS testing: {latest['version']} ({latest['build']}) {latest['internal_state'].lower()}")
    release = (play or {}).get("tracks", {}).get(config["google_play"]["track"], {}).get("releases", [])
    if release:
        print(f"  Android testing: {release[0]['name']}, version code {', '.join(release[0]['versionCodes'])}, "
              f"{release[0]['status']}")


def shared_jamf_page():
    """A signed link to demo 6's Jamf stand-in page, or None without it."""
    import jamf_apps

    if not jamf_apps.SHARED.exists() or os.environ.get("MOBILE_STANDIN_DIR") or "jamf" not in mobile.standins():
        return None
    module = jamf_apps.shared_module()
    ssm = mobile.ssm_client()
    url = mobile.get_parameter(ssm, "jamf/standin-url").rstrip("/")
    token = module.Signer(mobile.get_parameter(ssm, "jamf/standin-signing-key")).sign("ui", module.UI_SECONDS)
    return f"{url}/standin/ui/login?token={token}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--summary", action="store_true", help="print the stand-ins' state and exit")
    args = parser.parse_args()
    store = mobile.state_store()
    if args.summary:
        summary(store)
        return
    jamf_link = shared_jamf_page()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0].split("#")[0]
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/jamf")
                self.end_headers()
                return
            if path == "/jamf" and jamf_link:
                self.send_response(302)
                self.send_header("Location", jamf_link)
                self.end_headers()
                return
            if path not in PAGES:
                self.send_error(404)
                return
            body = page(path, store).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print(f"Stand-in console for Jamf Pro, TestFlight and Google Play: their state in {store.where()}, "
          f"on http://127.0.0.1:{args.port}/jamf, /testflight and /play. Ctrl-C stops it.", flush=True)
    try:
        http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
