"""One view of the CI run a stage script runs in, whichever CI runs it.

    import ci_env
    run = ci_env.current()
    run.name, run.run_id, run.run_url, run.build_id, run.commit, run.correlation_id

The stage scripts read the run's id, link, build id, commit and correlation id
here, so the same scripts run from Jenkins, GitHub Actions or any other CI.
Three routes, tried in this order:

- Neutral names, for a CI whose pipeline file sets them (GitLab CI, Azure
  Pipelines). With RELEASE_CI_NAME set, every value comes from
  RELEASE_CI_NAME (the link label, for example "GitLab pipeline"),
  RELEASE_RUN_ID, RELEASE_RUN_URL, RELEASE_BUILD_ID, RELEASE_COMMIT and
  RELEASE_CORRELATION_ID, and nothing else.
- GitHub Actions, from the variables GitHub sets: build id gh-<run number>,
  so JFrog paths never clash with Jenkins builds; the run's page; the
  commit; and the correlation id github-actions:<repository>:<run id>, which
  the approval bridge finds the change by. The requester is the people.yaml
  releaser whose github_login merged (Sam), and the run counts from the
  merge commit's time, so Slack's "Live in" starts at the merge. GitHub's
  runners can't reach Splunk on the tools host, deploy with the job's own
  AWS role, and mask every secret a script reads, since the logs are public.
- Jenkins, as before: BUILD_NUMBER, BUILD_URL and GIT_COMMIT. Outside any CI,
  a run by hand, the build is "local".
"""

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Run:
    ci: str                     # jenkins, github-actions or neutral
    name: str                   # the run's link label in the change and its notes
    run_id: str
    run_url: str
    build_id: str               # JFrog's folder and Slack's "Build #<id>"
    commit: str
    correlation_id: str = ""    # the change's correlation id; none under Jenkins
    actor: str = ""             # who started it, as the CI names them (GitHub: who merged)
    splunk: bool = True         # events go to Splunk's collector on the tools host
    job_role: bool = False      # deploy with the job's own AWS credentials
    masks: bool = False         # secrets read from SSM are masked in the log
    request_time_is_commit: bool = False  # the run counts from its commit's time
    origin: str = ""            # how it started, where that isn't Slack: the change's description says so


def current(environ=None):
    env = os.environ if environ is None else environ
    if env.get("RELEASE_CI_NAME"):
        return Run(ci="neutral", name=env["RELEASE_CI_NAME"], run_id=env.get("RELEASE_RUN_ID", ""),
                   run_url=env.get("RELEASE_RUN_URL", ""), build_id=env.get("RELEASE_BUILD_ID", "") or "local",
                   commit=env.get("RELEASE_COMMIT", ""), correlation_id=env.get("RELEASE_CORRELATION_ID", ""))
    if env.get("GITHUB_ACTIONS") == "true":
        repository, run_id = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        return Run(ci="github-actions", name="GitHub Actions run", run_id=run_id,
                   run_url=f"{server}/{repository}/actions/runs/{run_id}",
                   build_id=f"gh-{env.get('GITHUB_RUN_NUMBER', '')}", commit=env.get("GITHUB_SHA", ""),
                   correlation_id=f"github-actions:{repository}:{run_id}", actor=env.get("GITHUB_ACTOR", ""),
                   splunk=False, job_role=True, masks=True, request_time_is_commit=True,
                   origin="by a merged pull request")
    return Run(ci="jenkins", name="Jenkins build", run_id=env.get("BUILD_NUMBER", ""),
               run_url=env.get("BUILD_URL", ""), build_id=env.get("BUILD_NUMBER", "local"),
               commit=env.get("GIT_COMMIT", ""))


def requester(run=None, everyone=None):
    """The Slack member id of who asked, where the CI knows it: on GitHub the
    releaser in people.yaml whose github_login merged. None elsewhere, where
    the Slack handler passes it as a parameter."""
    run = run or current()
    if run.ci != "github-actions" or not run.actor:
        return None
    if everyone is None:
        from common import people

        everyone = people()
    for person in everyone.values():
        if person.get("releaser") and person.get("github_login") == run.actor and person.get("slack_user_id"):
            return person["slack_user_id"]
    return None


def mask(value, run=None, out=None):
    """Hide a secret in a CI's public log before anything can print it: GitHub
    masks what it is told to, the whole value and, for a key, each of its
    lines. A no-op on CIs whose logs aren't public."""
    run = run or current()
    if not run.masks or not value:
        return
    out = out or sys.stdout
    for part in dict.fromkeys([value, *value.splitlines()]):
        if part.strip():
            # A workflow command's data escapes %, CR and LF.
            data = part.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            out.write(f"::add-mask::{data}\n")
    out.flush()
