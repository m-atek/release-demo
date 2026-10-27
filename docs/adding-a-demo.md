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

The shared scripts (`change.py`, `notify.py`, `events.py`, `jira.py`,
`tag.py`, `publish.py`) are the same for every service. A demo doesn't
change them; if it needs something new from them, keep it optional and
backward compatible.

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
