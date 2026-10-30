"""The sample's pages: evidence-sample.html, one evidence/<change>.html per
change, and evidence/style.css. Plain HTML: no script and no inline style,
so a page renders under Jenkins' content security policy for build
artifacts, and every link opens in the same tab. Link texts name the record,
never an instance's address. The words stay plain: the pages answer five
questions from the records and claim nothing more.
"""

import html
import urllib.parse

from sample import QUESTIONS

JFROG_UI = "https://artifactory-demo.es.nl/ui/repos/tree/General/"
SPLUNK = "https://splunk-demo.es.nl/en-US/app/search/search?q="

STYLE = """:root { --ink: #14181f; --muted: #5b6472; --line: #dfe3e8; --paper: #ffffff; --wash: #f5f6f8;
  --yes: #047857; --pre: #1d4ed8; --design: #6d28d9; --no: #b91c1c; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--wash); color: var(--ink);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { margin: 0 auto; max-width: 1500px; padding: 28px 24px 40px; }
h1 { font-size: 28px; margin: 0 0 6px; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 0 0 6px; }
p { margin: 6px 0; }
.header { font-size: 18px; }
.rule, .left-out, .meta, .small { color: var(--muted); }
table { width: 100%; border-collapse: collapse; background: var(--paper); border: 1px solid var(--line); margin: 16px 0; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--line); }
th { font-size: 13px; color: var(--muted); font-weight: 600; }
td.answer { font-weight: 600; white-space: nowrap; }
.yes { color: var(--yes); }
.pre-approved { color: var(--pre); }
.by-design { color: var(--design); }
.no { color: var(--no); }
.under { display: block; font-weight: 400; color: var(--muted); font-size: 13px; white-space: normal; }
.summary { font-size: 18px; font-weight: 600; }
section { background: var(--paper); border: 1px solid var(--line); padding: 14px 16px; margin: 12px 0; }
code { font-family: ui-monospace, Menlo, monospace; font-size: 14px; word-break: break-all; }
ul.hashes { list-style: none; padding: 0; margin: 6px 0; }
ul.hashes li { margin: 4px 0; }
a { color: #1d4ed8; }
"""


def css_class(text):
    return {"yes": "yes", "pre-approved": "pre-approved", "by design": "by-design"}.get(text, "no")


def esc(text):
    return html.escape(str(text))


def link(href, text):
    return f'<a href="{esc(href)}">{esc(text)}</a>' if href else esc(text)


def page(title, body, stylesheet):
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>{esc(title)}</title>\n'
            f'<link rel="stylesheet" href="{stylesheet}">\n</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n')


def rule_text(seed, size):
    return (f"How these {size} were drawn: each change's number is put after the seed, as \"{seed}:CHG0030041\", "
            f"and ranked by the SHA-256 of that text, lowest first. The first {size} are the sample, listed by "
            f"number. To redo it by hand: printf '{seed}:CHG0030041' | shasum -a 256, for each change.")


def summary_line(rows):
    meets = [row for row in rows if all(answer.meets for answer in row["answers"])]
    pre = sum(1 for row in rows if row["kind"] in ("standard", "rollback"))
    return f"Meets all five: {len(meets)} of {len(rows)} ({pre} pre-approved by template). Exceptions: {len(rows) - len(meets)}."


def index(rows, header, rule, left_out):
    cells = []
    for row in rows:
        answers = "".join(f'<td class="answer {css_class(a.text)}">{esc(a.text)}</td>' for a in row["answers"])
        approved = esc(row["approved_by"])
        if row["kind"] in ("standard", "rollback"):
            approved = f'pre-approved<span class="under">{esc(row["template"] or "")}</span>'
        cells.append(f'<tr><td>{link("evidence/" + row["number"] + ".html", row["number"])}</td>'
                     f'<td>{esc(row["version"])}</td><td>{esc(row["requested_by"])}</td><td>{approved}</td>'
                     f'{answers}<td>{esc(row["result"])}</td></tr>')
    heads = "".join(f"<th>{esc(q)}</th>" for q in QUESTIONS)
    left = ", ".join(f"{count} {why}" for why, count in left_out.items())
    body = (f"<h1>Evidence sample</h1>\n<p class=\"header\">{esc(header)}</p>\n<p class=\"rule\">{esc(rule)}</p>\n"
            f"<table>\n<thead><tr><th>Change</th><th>Version</th><th>Requested by</th><th>Approved by</th>{heads}"
            f"<th>Result</th></tr></thead>\n<tbody>\n" + "\n".join(cells) + "\n</tbody>\n</table>\n"
            f"<p class=\"summary\">{esc(summary_line(rows))}</p>\n<p class=\"left-out\">Left out of the population: {esc(left)}.</p>")
    return page("Evidence sample: orders-service", body, "evidence/style.css")


def change_page(row):
    sections = []
    for question, answer in zip(QUESTIONS, row["answers"]):
        if question.startswith("3.") and answer.text != "by design":
            lines = "<ul class=\"hashes\">" + "".join(
                f"<li>{esc(line.split(': ', 1)[0])}: <code>{esc(line.split(': ', 1)[1])}</code></li>"
                for line in answer.lines) + "</ul>"
        else:
            lines = "".join(f"<p>{esc(line)}</p>" for line in answer.lines)
        links = " · ".join(link(href, text) for href, text in answer.links)
        sections.append(f'<section>\n<h2>{esc(question)}: <span class="{css_class(answer.text)}">{esc(answer.text)}'
                        f'</span></h2>\n{lines}\n{f"<p>{links}</p>" if links else ""}\n</section>')
    meta = (f"Requested by {row['requested_by']} · {row['kind_label']} · closed {row['close_code']} · "
            f"result: {row['result']}")
    bottom = " · ".join(link(href, text) for href, text in row["links"] + [("../evidence-sample.html", "Back to the sample")])
    body = (f"<h1>{esc(row['number'])}: orders-service {esc(row['version'])} ({esc(row['jira_key'])})</h1>\n"
            f"<p class=\"meta\">{esc(meta)}</p>\n" + "\n".join(sections) + f"\n<p>{bottom}</p>\n"
            "<p class=\"small\">Times in UTC. Every value above is read from the record its link opens.</p>")
    return page(f"{row['number']}: orders-service {row['version']}", body, "style.css")


def jfrog_link(path):
    return JFROG_UI + path if path else None


def splunk_link(number):
    return SPLUNK + urllib.parse.quote(f'search sourcetype="release:event" change="{number}" '
                                       "| table _time stage result seconds environment | sort _time")
