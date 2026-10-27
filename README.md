# release-demo

One release flow, end to end, in the tools an enterprise engineering team runs: a release started from Slack, quality and security gates, one package stored once and deployed by checksum, a ServiceNow change approved by someone other than the requester, status in Slack, Jira and Grafana, and an automatic rollback recorded like any other change.

This is a demo system. It runs no client's code and holds no client's data.

## Status

Built so far, and runnable on a Mac with no accounts:

- `services/orders-service/`: the app the pipeline ships, an AWS Lambda function behind a function URL.
- `demo-states/orders-service/`: every version a take releases, plus three broken variants for the acceptance checks.
- `scripts/`: stage, build and check, the local runner, `tools.sh`, `jcr_setup.py` and shared helpers.
- `tools-host/` and `jenkins/`: the tools host (Jenkins configured as code, JFrog Container Registry on PostgreSQL, Splunk Free), runnable locally in Docker.
- `demo-data/release-checklist.html`: the fictional checklist for the video's first shot.

Still to build: the EC2 tools host (Terraform in `infra/`, the boot script, the idle stop, the Cloudflare tunnel), the release pipeline (Jenkinsfile, shared library and the remaining stage scripts), the Slack app, the ServiceNow, Jira, SonarQube and Grafana integrations, `reset.py`, `history.py` and the RUNBOOK.

## Local development

Needs Python 3.11 or later. The function targets Lambda's `python3.13` runtime and uses only the standard library.

```bash
make setup                           # .venv with pytest and PyYAML
make stage V=1.3.0                   # put the baseline in services/orders-service
make test                            # the service's tests
make dev                             # staging on :8001, production on :8002
```

`make dev` runs both environments side by side and reloads on save. Each environment gets only the settings `services.yaml` gives it, as its Lambda function does, so configuration drift shows up locally first. Try the release that fails in production:

```bash
make dev STATE=1.4.1                 # in one terminal
make check ENV=staging V=1.4.1       # passes
make check ENV=production V=1.4.1    # fails: / answers 502
```

1.4.1 reads `DELIVERY_ESTIMATE_DAYS`. Staging has it; production still has the old name, `DELIVERY_DAYS`.

Other targets:

| Command | What it does |
| --- | --- |
| `make test-states` | Runs every demo state's tests. `1.4.0-broken-test` is meant to fail |
| `make build` | Packages the service into `dist/<service>/`, with `build-info.json` and its SHA-256. The same source gives the same checksum |
| `scripts/dev.py orders-service --package dist/...zip` | Serves a built package instead of the working copy |
| `scripts/stage.py orders-service 1.4.0` | Stages a version and commits "demo: stage orders-service 1.4.0 [skip ci]", then pushes if `origin` exists. `make stage` only copies |

## Tools host, locally

The same Docker Compose stack the EC2 tools host runs, on your Mac, with the Jenkins agent in a container. Needs Docker Desktop with at least 8 GB of memory.

```bash
scripts/tools.sh up --local          # build, start, wait until everything answers, then "Ready"
scripts/tools.sh status --local
scripts/tools.sh down --local        # stop; the data stays in Docker volumes
```

| Service | Local address | Notes |
| --- | --- | --- |
| Jenkins | http://localhost:8080 | User `admin`. The password is in `~/.local/state/release-demo/secrets/jenkins-admin-password` |
| JFrog Container Registry | http://localhost:8082 | On PostgreSQL: JCR 7.161 doesn't start on its built-in database |
| Splunk Free | http://localhost:8000 | No login. HTTP Event Collector on :8088. amd64 only, so it runs under emulation on Apple silicon |

Secrets are made once and kept in `~/.local/state/release-demo/secrets`, outside the repository. On the EC2 host they will come from SSM onto a tmpfs.

The first time:

1. `scripts/tools.sh up --local` asks you to accept Splunk's General Terms. Splunk starts only after a yes; everything else starts either way.
2. Open http://localhost:8082, accept JCR's licence and set the admin password in its setup wizard. Skip its repository step.
3. Run `.venv/bin/python scripts/jcr_setup.py --local`. It creates the repositories, the user `release-pipeline` with read and deploy rights but no delete, and the token Jenkins uses.
4. In Jenkins, run `tools-smoke`. It checks the agent, a pinned toolchain container, JCR and Splunk.

Jenkins is configured only through `jenkins/casc.yaml` and the pinned `jenkins/plugins.txt`. To change a setting, edit the file and reload it: Manage Jenkins, Configuration as Code, Reload existing configuration.

## Demo states

| State | Jira | What changes | Where it stops |
| --- | --- | --- | --- |
| 1.1.0 | | What Terraform's first apply deploys | |
| 1.3.0 | | Production before the recording | |
| 1.4.0 | ORD-12 | Adds "Delivery estimate: 2 days" to the page. Normal change | |
| 1.4.1 | ORD-13 | Reads the estimate from the regional setting. Standard change | Production check, then rolls back to 1.4.0 |
| 1.4.0-broken-test | | /health reports the version with a "v" prefix | Tests |
| 1.4.0-broken-gate | | Untested code with a bug and code smells | Quality gate |
| 1.4.0-broken-scan | | Declares urllib3 1.22 (CVE-2018-20060, critical) | SBOM and scan |

The versions 1.2.0 to 1.3.0 that `history.py` releases come with that script.

## Layout

```
services.yaml            per service: platform, environments, settings, checks, ServiceNow and SonarQube keys
people.yaml              Sam, Alex and Mateusz across Slack, ServiceNow and GitHub
services/orders-service/ handler.py, VERSION, tests/
demo-states/             the prepared source of every version a demo releases
scripts/                 one Python script per stage, plus stage.py, tools.sh and the local runner
jenkins/                 controller image, pinned plugins, casc.yaml, and jobs/
tools-host/              docker-compose.yml for the tools host, the local override and the local agent image
demo-data/               the fictional release checklist
```

## License

MIT
