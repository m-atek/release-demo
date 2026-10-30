"""The secrets inventory: which of the platform's secrets rotate by script, which still by hand.

    python scripts/secrets.py inventory           print it
    python scripts/secrets.py inventory --html    also write dist/secrets-inventory.html

Every secure entry in infra/parameters.yaml has an entry in
infra/secret-rotation.yaml: credential: false (a URL, an id, an email:
nothing to rotate), or rotation: by script (scripts/rotate.py <rotator>)
or by hand (how). The page has one row per stored credential:

- Secret: its name in SSM, under /release-demo/.
- Lives in: SSM, plus the tools host's tmpfs unless host_secrets.py leaves
  it off, plus Jenkins where casc.yaml reads it.
- Used by: the files in this repository that read it, from a search: the
  first two and how many more.
- Rotates: by script, or by hand and where.
- Changed: SSM's last modified time, read from metadata: nothing is decrypted.

By-script rows first. A credential whose value isn't set yet is counted
apart, under the table.
"""

import datetime
import html
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from common import ROOT

ROTATION = ROOT / "infra" / "secret-rotation.yaml"
CATALOG = ROOT / "infra" / "parameters.yaml"
CASC = ROOT / "jenkins" / "casc.yaml"
HOST_SECRETS = ROOT / "tools-host" / "host_secrets.py"
PAGE = ROOT / "dist" / "secrets-inventory.html"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
# Files that describe secrets rather than read them.
NOT_READERS = {"infra/parameters.yaml", "infra/secret-rotation.yaml", "scripts/inventory.py"}
READER_SUFFIXES = (".py", ".sh", ".groovy", ".tf", ".tftpl", ".yaml", ".yml", ".ps1", "Jenkinsfile")


def catalog():
    return yaml.safe_load(CATALOG.read_text())


def rotations():
    return yaml.safe_load(ROTATION.read_text()) or {}


def secure_names():
    return [name for name, entry in catalog().items() if entry.get("secure")]


def unclassified():
    """Secure parameters with neither credential: false nor rotation: the test fails on these."""
    known = rotations()
    return [name for name in secure_names()
            if not (known.get(name, {}).get("credential") is False or known.get(name, {}).get("rotation"))]


def not_on_host():
    text = HOST_SECRETS.read_text()
    block = re.search(r"NOT_ON_HOST = \{(.*?)\}", text, re.S).group(1)
    return set(re.findall(r'"([^"]+)"', block))


def casc_reads():
    """casc.yaml's ${name} values: {tmpfs file name: what reads it}."""
    found = {}
    text = CASC.read_text()
    for match in re.finditer(r"\$\{([a-z0-9-]+)(?::-[^}]*)?\}", text):
        before = text[:match.start()]
        ids = re.findall(r"\bid: ([\w-]+)", before)
        found.setdefault(match.group(1), f"credential {ids[-1]}" if ids else "configuration")
    return found


def tracked_files():
    result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    files = result.stdout.split() if result.returncode == 0 else [
        str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [f for f in files if f.endswith(READER_SUFFIXES) and f not in NOT_READERS
            and "/tests/" not in f and not f.startswith(("demo-states/", "docs/"))]


def used_by(name, files=None):
    """The files that name the parameter, or its tmpfs file, as a whole word."""
    dashed = name.replace("/", "-")
    pattern = re.compile(rf"(?<![\w/-])({re.escape(name)}|{re.escape(dashed)})(?![\w/-])")
    found = []
    for relative in files if files is not None else tracked_files():
        try:
            text = (ROOT / relative).read_text(errors="replace")
        except OSError:
            continue
        if pattern.search(text):
            found.append(relative)
    return sorted(found)


def used_by_text(files):
    if not files:
        return "nothing in the repository reads it by name"
    shown = ", ".join(files[:2])
    return shown + (f" and {len(files) - 2} more" if len(files) > 2 else "")


def lives_in(name, host_off=None, casc=None):
    host_off = not_on_host() if host_off is None else host_off
    casc = casc_reads() if casc is None else casc
    places = ["SSM"]
    if name not in host_off:
        places.append("tools host tmpfs")
    dashed = name.replace("/", "-")
    reader = casc.get(dashed) or next((what for key, what in casc.items() if key.startswith(dashed + "-")), None)
    if reader:
        places.append(f"Jenkins ({reader})")
    return places


def rotates_text(rotation):
    if rotation["by"] == "script":
        return f"By script: scripts/rotate.py {rotation['rotator']}"
    return f"By hand: {rotation['how']}"


def rows(metadata=None, files=None):
    """One dict per credential, by-script first. metadata: {name: {"version", "modified"}} from SSM,
    or None when there is no AWS sign-in (then every credential counts as stored)."""
    entries, known = catalog(), rotations()
    host_off, casc = not_on_host(), casc_reads()
    files = tracked_files() if files is None else files
    result = []
    for name in secure_names():
        rotation = known.get(name, {}).get("rotation")
        if not rotation:
            continue
        meta = (metadata or {}).get(name)
        stored = metadata is None or (meta is not None and (generated(entries[name]) or meta["version"] > 1))
        result.append({"name": name, "lives_in": lives_in(name, host_off, casc), "used_by": used_by(name, files),
                       "by": rotation["by"], "rotates": rotates_text(rotation), "stored": stored,
                       "changed": meta["modified"] if meta else None})
    return sorted(result, key=lambda row: (row["by"] != "script", row["name"]))


def generated(entry):
    """Terraform writes this one's value itself, as version 1. The other names
    hold "unset" until someone or a script writes the value, as version 2 or later."""
    return entry["source"] == "terraform" and not re.search(r"[Ww]ritten by scripts/|writes the value", entry["help"])


def ssm_metadata(ssm):
    """Name to version and last modified time, from describe_parameters: nothing decrypted."""
    found = {}
    pages = ssm.get_paginator("describe_parameters").paginate(
        ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": ["/release-demo/"]}])
    for page in pages:
        for parameter in page["Parameters"]:
            found[parameter["Name"].removeprefix("/release-demo/")] = {
                "version": parameter["Version"], "modified": parameter["LastModifiedDate"]}
    return found


def summary(all_rows):
    stored = [r for r in all_rows if r["stored"]]
    script = sum(r["by"] == "script" for r in stored)
    return len(stored), script, len(stored) - script


def when(moment):
    return moment.astimezone(AMSTERDAM).strftime("%d %b %Y, %H:%M") if moment else "unknown"


def print_inventory(all_rows, out=sys.stdout):
    total, script, hand = summary(all_rows)
    known = rotations()
    print(f"{total} stored secrets · {script} rotate by script · {hand} rotate by hand", file=out)
    for row in all_rows:
        mark = "" if row["stored"] else "  (not set, not counted)"
        print(f"  {row['name']:<42} {row['rotates'][:70]}{mark}", file=out)
    not_credentials = [n for n in secure_names() if known.get(n, {}).get("credential") is False]
    print(f"Not credentials, nothing to rotate: {len(not_credentials)} ({', '.join(not_credentials)})", file=out)


STYLE = """
  :root { --ink: #14181f; --muted: #5b6472; --line: #e3e6eb; --paper: #ffffff; --wash: #f5f6f8;
    --script: #047857; --hand: #b45309; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--wash); color: var(--ink);
    font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 1280px; margin: 0 auto; padding: 40px 32px; }
  h1 { margin: 0 0 8px; font-size: 34px; letter-spacing: -0.02em; }
  .summary { font-size: 22px; margin: 0 0 28px; }
  .summary b { font-variant-numeric: tabular-nums; }
  table { width: 100%; border-collapse: collapse; background: var(--paper); border: 1px solid var(--line); }
  th, td { text-align: left; vertical-align: top; padding: 10px 14px; border-bottom: 1px solid var(--line); }
  th { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  td.secret { font-family: ui-monospace, Menlo, monospace; font-size: 15px; white-space: nowrap; }
  td.rotates.script { color: var(--script); font-weight: 600; }
  td.rotates.hand { color: var(--hand); }
  .small { color: var(--muted); font-size: 14px; }
  .note { color: var(--muted); margin-top: 18px; font-size: 15px; }
"""


def render(all_rows, generated=None):
    total, script, hand = summary(all_rows)
    generated = generated or datetime.datetime.now(datetime.timezone.utc)
    body = []
    for row in (r for r in all_rows if r["stored"]):
        users = row["used_by"]
        used = html.escape(", ".join(users[:2]) or "nothing reads it by name")
        more = f'<br><span class="small">and {len(users) - 2} more</span>' if len(users) > 2 else ""
        body.append(
            f'<tr><td class="secret">{html.escape(row["name"])}</td>'
            f'<td>{html.escape(", ".join(row["lives_in"]))}</td>'
            f"<td>{used}{more}</td>"
            f'<td class="rotates {row["by"]}">{html.escape(row["rotates"])}</td>'
            f"<td>{html.escape(when(row['changed']))}</td></tr>")
    unset = [r["name"] for r in all_rows if not r["stored"]]
    note = (f'<p class="note">Not counted: {len(unset)} more credentials in infra/parameters.yaml that have no '
            f"value yet ({html.escape(', '.join(unset))}).</p>") if unset else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Secrets inventory</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>Secrets inventory</h1>
<p class="summary"><b>{total}</b> stored secrets · <b>{script}</b> rotate by script · <b>{hand}</b> rotate by hand</p>
<table>
<thead><tr><th>Secret</th><th>Lives in</th><th>Used by</th><th>Rotates</th><th>Changed</th></tr></thead>
<tbody>
{chr(10).join(body)}
</tbody>
</table>
{note}
<p class="note">From infra/parameters.yaml, infra/secret-rotation.yaml and SSM's metadata, {html.escape(when(generated))}. No value was read.</p>
</main>
</body>
</html>
"""


def main(ssm=None, write_html=False):
    metadata = ssm_metadata(ssm) if ssm else None
    all_rows = rows(metadata)
    print_inventory(all_rows)
    if write_html:
        PAGE.parent.mkdir(parents=True, exist_ok=True)
        PAGE.write_text(render(all_rows))
        print(f"Wrote {PAGE.relative_to(ROOT)}")
