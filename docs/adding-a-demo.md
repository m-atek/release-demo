# Adding a demo

Every demo releases its own service through the same pipeline: the same
Slack start, gates, ServiceNow change, Jira version, events and rollback.
A demo adds only what is specific to its target. This page is the contract
between the shared platform and a demo.

## 1. The service in services.yaml

One entry per service. The keys the shared pipeline reads:

```yaml
store-sync:
  platform: windows-fleet          # free text, shown in docs and Grafana
  path: services/store-sync        # the code, with a VERSION file at its root
  toolchain: jenkins/toolchain/dotnet.Dockerfile   # optional, default python.Dockerfile
  build_agent: windows             # optional Jenkins label; default: build in the
                                   # toolchain container on the tools host
  scripts:                         # every stage script, run from the repository root
    build: scripts/demos/store_sync/build.py
    test: scripts/demos/store_sync/test.sh      # optional, default: pytest in path/tests
    publish: scripts/publish.py
    deploy: scripts/demos/store_sync/deploy.py
    check: scripts/demos/store_sync/check.py
  jira: { project: ORD }
  slack: { options: [standard] }
  servicenow:
    cmdb_item: store-sync
    assignment_group: Platform team
    approvers: Change approvers
    templates: { standard: Routine release of store-sync, rollback: Rollback of store-sync }
  sonarqube:
    project_key: m-atek_release-demo_store-sync
    properties: { sonar.cs.vstest.reportsPaths: .release/reports/*.trx }   # optional extras
  environments:
    staging: { ... }               # whatever the demo's scripts need
    production: { ... }
```

Optional keys, each with its default where it is missing:

| Key | What it does |
| --- | --- |
| no `environments.staging` | Staging and Check staging show as skipped; the deploy script is never called for staging (demos 1 and 3). |
| `scripts.plan` | A Plan stage before the change, `<plan> <service>`, for what the change needs from the target (demo 1's waves). |
| `scripts.quality_gate` | The service's own analysis instead of the shared sonar-scanner, `<quality_gate> <service> <version>` in its toolchain, with `SONAR_TOKEN` and `SONAR_ORGANIZATION` set (demo 1's SonarScanner for .NET). |
| `build_agents` | `{<label>: {build: <command>, test: <command>}}`: each runs first, in parallel on that agent (`sh`, or PowerShell on Windows), and hands back `dist/<service>/parts/<label>/`; then `scripts.build` and `scripts.test` run on the tools host (demo 6). |
| `jenkins_credentials` | `{<credential id>: <variable>}`, bound around every deploy and check, in the release, the rollback and the reset job (demo 5's ID tokens). |
| `servicenow.standard_options` | The Slack options that make a standard change; default `[standard]` (demo 7: `[stores]`). |
| `servicenow.servers` | CI names, of any class, added to the change as affected items when it opens (demo 3). |
| `servicenow.template_texts` | `{<template kind>: {field: text}}`, the texts `servicenow_setup.py` gives that template instead of the shared ones (demo 6). |

## 2. The stage scripts

The pipeline calls each script with the same arguments as the Lambda
service's scripts, from the repository root, and reads and writes the run
state through `scripts/run_state.py` (`.release/state.json`).

| Stage | Call | Must do |
| --- | --- | --- |
| Build | `<build> <service> --jira-key ORD-12` | Write `dist/<service>/<service>-<version>.zip` (any content: the artifact, installer or manifests) and `dist/<service>/build-info.json` with `sha256` of the zip. The same source gives the same checksum where the toolchain allows it. |
| Test | `<test> <service>` | Write `.release/reports/junit.xml`, and a coverage report where SonarQube supports the language. Exit non-zero on a failed test. |
| Publish | `scripts/publish.py <service>` | Shared. Uploads the zip and build-info.json to JFrog. |
| Deploy | `<deploy> <service> <environment>` | Deploy the exact published package: download it from JFrog (`publish.download`), verify its SHA-256 against the run state, deploy, and set `deploys.<environment>` in the run state: `number` (what was deployed), `previous_number`, `previous_version`, `url` (what the check calls, if anything), `deployed_at`. |
| Deploy back | `<deploy> <service> <environment> --back` | Return to `previous_*` from this run. Set `rolled_back_to`. |
| Deploy previous | `<deploy> <service> <environment> --previous` | Return to the release before the live one (the Slack /rollback command). |
| Check | `<check> <service> <environment> --version <v> --url <url>` | Exit 0 when the new version is live and working, non-zero otherwise. Print one line per check: `ok   <what>` or `FAIL <what>`. |

When a release stops, Slack's last line says where. A deploy script may set
`finish_line` in the run state (`python scripts/run_state.py set "finish_line=Wave stopped at STORE-004 after {total}. STORE-006 was not touched; the others run 3.3.0."`)
to say how its stop reads; `{total}` becomes the run's total time.

What else a stage script may put in the run state for the shared scripts:

- `change_extras`: `fields` for the change and `affected_cis` (CMDB sys_ids), which `change.py open` adds while the change is still New: ServiceNow refuses affected items once it is in Assess. Set it before the Change stage, in staging or the Plan stage.
- `change_details`: lines added to the change's description, so the approver sees them.
- `rows`: lines Slack's message shows under the stages (demo 1's waves).
- `change.closed`: the deploy script closed the change itself (demo 1's stop rule), so the pipeline's stop leaves it alone. Post Slack's last line first (`notify.py finish failed`, which sets `total`) and give that `total` in the close notes, so the change and Slack name the same time; with `total` set, the pipeline's stop leaves Slack alone too.
- `check.notes`: what the change's close notes start with after a rollback.

The shared scripts (`change.py`, `notify.py`, `events.py`, `jira.py`,
`tag.py`, `publish.py`) are the same for every service. A demo doesn't
change them; if it needs something new from them, keep it optional and
backward compatible.

JFrog: add `jcr: { shared: true }` to the entry. Its packages then go to
`demos-candidates/<service>/<version>/<build>/` and
`demos-releases/<service>/<version>/` (use `publish.candidates_root(service)`
and `publish.releases_root(service)` in your scripts), because JCR's free
edition creates repositories only in its UI, and one shared pair is created
once.

## 3. Its infrastructure

- Terraform in `infra/demos/<n>-<name>/`, its own root and state:
  backend key `release-demo/demos/<n>-<name>.tfstate` in the same bucket,
  `scripts/tf.sh` style init. Default tags `Project = release-demo`,
  `Demo = <n>-<name>`.
- `scripts/demo.sh up <name>` creates or starts what the demo needs and waits
  until it answers; `scripts/demo.sh down <name>` stops it so it costs
  nothing overnight (stop, don't destroy, where the data matters).
- Secrets go to SSM through `infra/parameters.yaml` and `scripts/secrets.py`,
  never into the repository.
- Cloudflare stays as it is: demos add no DNS records or Access apps.

## 4. Its demo states and reset

- `demo-states/<service>/<version>/` holds each version the video needs,
  staged on main with `scripts/stage.py <service> <version>`.
- `scripts/reset.py <demo>` puts the demo back to its starting state in under
  two minutes, safe to run twice.

## 5. Stand-ins for slow outside services

Apple notarization and TestFlight, Google Play, Microsoft's signing
validation and Jamf may run as stand-ins (`STANDIN=1` in the service's
settings), because their accounts, reviews or trials take days or weeks. A
stand-in behaves like the real API (same requests, realistic answers, a
rejection the video can show) and says what it is in its startup log. The
real API sits behind the same interface, so switching is a setting.
