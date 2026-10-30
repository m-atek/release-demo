"""The checks that decide whether Claude's text reaches anyone (videos 12 and 13).

    summary_failures(answer, evidence)        [] when the summary may go out, else what didn't match
    check_quotes(answer, sources)             each quote kept or dropped, word for word against its source
    unknown_names(text, known)                settings, files and versions a text names that the sources don't

Summary (video 12): every number in a sentence is listed with an evidence key
whose value equals it, or is a version in the evidence, or appears in a line
the diff adds or removes; every setting name, file name and version is in the
evidence; each not_checked item cites an existing finding and contains its
setting name and environments, one item per finding; tests, scan and package
are all there. Any one failure withholds the whole summary.

Likely cause (video 13): a quote must equal one whole line of its source,
after trimming whitespace and a diff's leading + or -. A quote that isn't
found is dropped. The cause may name only settings, files and versions that
are in the sources.
"""

import re

VERSION = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?![\w]|\.\d)")
# Identifiers that hold digits but aren't numbers a sentence states.
IDENTIFIERS = [re.compile(pattern) for pattern in (
    r"SHA-?256", r"\b[A-Z][A-Z0-9]+-\d+\b", r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b", r"\b[A-Za-z]+\d+[\w]*\b")]
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\w|\.\d)")
SETTING = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
CAPITALS = re.compile(r"\b[A-Z][A-Z0-9_]+\b")
FILE = re.compile(r"\b[\w.-]+\.(?:py|json|xml|ya?ml|txt|toml|cfg|ini|html|md|zip)\b")
RESULTS = ["tests", "quality_gate", "scan", "package"]
SOURCES = ["cloudwatch", "diff", "settings", "check"]


def numbers_in(sentence):
    """The numbers a sentence states, versions and identifiers left out."""
    text = VERSION.sub(" ", sentence)
    for pattern in IDENTIFIERS:
        text = pattern.sub(" ", text)
    found = []
    for match in NUMBER.finditer(text):
        token = match.group().rstrip(",")
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return found


def lookup(evidence, key):
    """The value at a dotted key such as results.tests.passed or findings.0.line, or KeyError."""
    value = evidence
    for part in str(key).split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(key)
    return value


def same(a, b):
    return isinstance(a, (int, float)) and not isinstance(a, bool) and \
        isinstance(b, (int, float)) and not isinstance(b, bool) and abs(float(a) - float(b)) < 1e-9


def diff_numbers(evidence):
    """The numbers in the lines the diff adds or removes, not in its headers or context."""
    text = (evidence.get("diff") or {}).get("text") or ""
    changed = [line[1:] for line in text.splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    return {number for line in changed for number in numbers_in(line)}


def known_names(evidence, extra_files=("evidence.py", "evidence.json", "services.yaml")):
    """Every setting-like name, file name and version anywhere in the evidence,
    and the platform's own files the notes name."""
    text = "\n".join(strings(evidence))
    return {"settings": set(CAPITALS.findall(text)), "files": set(FILE.findall(text)) | set(extra_files),
            "versions": set(VERSION.findall(text))}


def strings(value):
    """Every string in a JSON value, keys included, as written: not JSON-escaped."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


def unknown_names(text, known):
    """The settings, files and versions a text names that known doesn't hold."""
    unknown = [f"setting {name}" for name in sorted(set(SETTING.findall(text)) - known["settings"])]
    unknown += [f"file {name}" for name in sorted(set(FILE.findall(text)) - known["files"])]
    unknown += [f"version {name}" for name in sorted(set(VERSION.findall(text)) - known["versions"])]
    return unknown


def item_failures(label, item, evidence, known, from_diff, diff_allowed):
    failures = []
    sentence = (item.get("sentence") or "").strip()
    if not sentence:
        return [f"{label}: missing"]
    listed = []
    for entry in item.get("numbers") or []:
        try:
            actual = lookup(evidence, entry.get("key"))
        except KeyError:
            failures.append(f"{label}: {entry.get('key')} isn't in the evidence")
            continue
        if not same(entry.get("value"), actual):
            failures.append(f"{label}: {entry.get('key')} is {actual}, not {entry.get('value')}")
            continue
        listed.append(float(entry["value"]))
    for number in numbers_in(sentence):
        if any(same(number, value) for value in listed) or (diff_allowed and number in from_diff):
            continue
        failures.append(f"{label}: {number:g} isn't in the evidence")
    failures += [f"{label}: unknown {name}" for name in unknown_names(sentence, known)]
    return failures


def summary_failures(answer, evidence):
    """What in a change summary doesn't match the evidence; empty when it all does."""
    known = known_names(evidence)
    from_diff = diff_numbers(evidence)
    failures = []
    what = answer.get("what_changes") or {}
    failures += item_failures("What changes", what, evidence, known, from_diff, diff_allowed=True)
    for name in what.get("names") or []:
        failures += [f"What changes: unknown {unknown}" for unknown in unknown_names(name, known)]
    results = answer.get("results") or {}
    for key in RESULTS:
        label = key.replace("_", " ").capitalize()
        failures += item_failures(label, results.get(key) or {}, evidence, known, from_diff, diff_allowed=False)

    findings = {finding["id"]: finding for finding in evidence.get("findings") or []}
    cited = []
    for item in answer.get("not_checked") or []:
        finding = findings.get(item.get("finding"))
        label = f"Not checked {item.get('finding') or '(no id)'}"
        if finding is None:
            failures.append(f"{label}: no such finding")
            continue
        cited.append(finding["id"])
        sentence = item.get("sentence") or ""
        if finding["setting"] not in sentence:
            failures.append(f"{label}: doesn't name {finding['setting']}")
        for environment in finding["missing_in"]:
            if not re.search(rf"\b{re.escape(environment)}\b", sentence, re.IGNORECASE):
                failures.append(f"{label}: doesn't name {environment}")
        failures += item_failures(label, item, evidence, known, from_diff, diff_allowed=True)
    for finding_id in findings:
        if cited.count(finding_id) == 0:
            failures.append(f"Not checked: no item for {finding_id}")
        elif cited.count(finding_id) > 1:
            failures.append(f"Not checked: {finding_id} twice")
    return failures


def normal(line, source):
    line = line.strip()
    if source == "diff" and line[:1] in "+-":
        line = line[1:].strip()
    return line


def source_lines(sources, source):
    """The candidate lines of one source, normalized as quotes are."""
    lines = sources.get(source) or []
    if source == "diff":
        lines = [line for line in lines if not line.startswith(("+++", "---", "@@"))]
    return {normal(line, source) for line in lines if line.strip()}


def check_quotes(answer, sources, limit=3):
    """([kept quotes], [dropped quotes]): each of the first `limit` evidence items,
    found word for word in its source or not."""
    kept, dropped = [], []
    for item in (answer.get("evidence") or [])[:limit]:
        source, quote = item.get("source"), normal(item.get("quote") or "", item.get("source"))
        if source in SOURCES and quote and quote in source_lines(sources, source):
            kept.append({"source": source, "quote": quote})
        else:
            dropped.append({"source": source, "quote": quote})
    return kept, dropped
