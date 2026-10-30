"""The stage scripts a release calls, in order, read from its pipeline's files,
so the tests can hold every other CI's release to Jenkins'.

    import pipeline_calls
    pipeline_calls.jenkins()                     the calls releasePipeline.groovy makes, in order
    pipeline_calls.calls(text)                   the calls in a workflow or pipeline file's scripts
    pipeline_calls.jenkins_stages()              the stages releasePipeline.groovy runs, in order

A call is the script and what it does, without the values: "deploy.py
staging", "deploy.py staging --back", "change.py open", "notify.py finish",
"pytest", "syft". What releaseStage.groovy does around every stage (Slack's
row, the event, the work note) and the run state's reads are left out:
stage_calls() holds those to releaseStage's. Jenkins' failure paths come in
the place they run: the staging rollback after Check staging, rollBack()
after Check production. stopped() is its own list, stopped_calls().
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "jenkins" / "shared-library" / "vars"

# What toolchain.groovy's steps and serviceConfig's defaults run, for orders-service.
TOOLCHAIN = {
    "build": ["build.py"],
    "test": ["pytest"],
    "qualityGate": ["sonar_args.py", "sonar-scanner"],
    "sbomAndScan": ["syft", "grype"],
}
# The calls a stage wrapper makes, compared on their own.
WRAPPER = ("notify.py stage", "change.py note")


def normalized(script, rest):
    """'deploy.py', ' orders-service staging --back' to 'deploy.py staging --back'."""
    words = [w.strip("'\"") for w in re.split(r"\s+", rest.strip()) if w]
    if script in ("deploy.py", "check.py"):
        environment = next((w for w in words if w in ("staging", "production")), "")
        back = " --back" if "--back" in words else ""
        return f"{script} {environment}{back}".strip()
    if script == "publish.py":
        return "publish.py --release" if "--release" in words else "publish.py"
    if script == "build.py":
        return "build.py"
    if script == "events.py":
        if words[:1] == ["annotate"]:
            return "events.py annotate"
        stage = words[1] if len(words) > 1 and words[0] == "--stage" else ""
        # A stage named by a variable is the wrapper's event; Rollback is rollBack's own.
        return f"events.py --stage {stage}" if re.fullmatch(r"[A-Z][a-z]+", stage) else "events.py --stage"
    sub = next((w for w in words if re.fullmatch(r"[a-z][a-z-]*", w)), "")
    return f"{script} {sub}".strip()


CALL = re.compile(
    r"scripts/(?P<script>\w+\.py)(?P<rest>[^\n;&|)]*)"           # python3 scripts/change.py open ...
    r"|(?P<pytest>-m pytest)"                                     # the service's test step
    r"|(?P<scanner>sonar-scanner)(?![\w.:-])"                     # sonar-scanner, not its image's name
    r"|(?P<syft>\$\{?SYFT\b|\$\{SYFT\})"                          # docker run ... $SYFT
    r"|(?P<grype>\$\{?GRYPE\b|\$\{GRYPE\})"
)


def calls(text, wrapper=False):
    """The calls in a file's scripts, in the order they appear. With wrapper,
    only the stage wrapper's calls; without, everything else."""
    found = []
    for match in CALL.finditer(text):
        if match.group("script"):
            script = match.group("script")
            if script == "run_state.py" and "init" not in match.group("rest"):
                continue  # the run state's reads and writes
            call = normalized(script, match.group("rest"))
        else:
            call = next(name if name != "scanner" else "sonar-scanner" for name in ("pytest", "scanner", "syft", "grype")
                        if match.group(name))
        is_wrapper = call.startswith(WRAPPER) or call == "events.py --stage"
        if is_wrapper == wrapper:
            found.append(call)
    return found


def groovy_function(text, name):
    """A Groovy function's body, by brace counting from its signature."""
    start = re.search(rf"^(?:\w[\w<>]* )+{name}\([^)]*\) \{{", text, re.M)
    if not start:
        raise ValueError(f"no function {name}")
    depth, index = 0, start.end() - 1
    for index in range(start.end() - 1, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            break
    return text[start.end():index]


def jenkins():
    """releasePipeline.groovy's calls for orders-service, with toolchain.groovy's
    steps, the deploy and check commands and rollBack() expanded where they run."""
    text = (LIBRARY / "releasePipeline.groovy").read_text()
    body = groovy_function(text, "call")
    body = body[:body.index("} catch (error)")]
    rollback = groovy_function(text, "rollBack")
    expansions = [
        (r"toolchain\.(build|test|qualityGate|sbomAndScan)\(", lambda m: TOOLCHAIN[m.group(1)]),
        (r"\$\{(deploy)\} \$\{service\} (\w+)( --back)?", lambda m: [f"deploy.py {m.group(2)}{m.group(3) or ''}"]),
        (r"\$\{serviceConfig\.command\(cfg, 'deploy'\)\} \$\{service\} (\w+)( --back)?",
         lambda m: [f"deploy.py {m.group(1)}{m.group(2) or ''}"]),
        (r"\bcheck\(cfg, service, '(\w+)'", lambda m: [f"check.py {m.group(1)}"]),
        (r"\$\{publish\} \$\{service\}", lambda m: ["publish.py"]),
        (r"\$PUBLISH \"\$SERVICE\" --release", lambda m: ["publish.py --release"]),
        (r"\brollBack\(cfg, service\)", lambda m: expand(rollback)),
        (r"scripts/\w+\.py[^\n;&|)']*", lambda m: calls(m.group(0))),
    ]

    def expand(source):
        found = []
        pattern = re.compile("|".join(f"(?:{p})" for p, _ in expansions))
        for match in pattern.finditer(source):
            for regex, make in expansions:
                inner = re.match(regex, match.group(0))
                if inner:
                    found += make(inner)
                    break
        return found

    return expand(body)


def jenkins_stages():
    """The stages releasePipeline.groovy runs for orders-service, in order:
    the ones it may skip included, the contract check and the plan left out."""
    text = groovy_function((LIBRARY / "releasePipeline.groovy").read_text(), "call")
    names = re.findall(r"releaseStage(?:\.skip)?\('([^']+)'", text)
    return [n for n in dict.fromkeys(names) if n not in ("Contract check", "Plan")]


def stopped_calls():
    """stopped()'s calls: Slack's last line, and the change closed or cancelled."""
    return calls(groovy_function((LIBRARY / "releasePipeline.groovy").read_text(), "stopped"))


def stage_calls():
    """What releaseStage.groovy does around each stage, and for a skipped one."""
    text = (LIBRARY / "releaseStage.groovy").read_text()
    around = calls(groovy_function(text, "call") + groovy_function(text, "finish"), wrapper=True)
    return around, calls(groovy_function(text, "skip"), wrapper=True)
