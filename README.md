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

On AWS so far: the Terraform state bucket, the SSM parameter names and the budget alert. Every account below exists and its values are in SSM.

Still to build: the rest of `infra/` (both functions, the EC2 tools host, the Slack handler, the Cloudflare tunnel and Access, the Grafana role), the tools host's boot script and idle stop, the release pipeline (Jenkinsfile, shared library and the remaining stage scripts), the ServiceNow, Jira, SonarQube and Grafana setup scripts, `reset.py`, `history.py` and the RUNBOOK.

## Setup on fresh accounts

You create every account, accept every licence and type every secret yourself. Secrets go into SSM Parameter Store through `scripts/secrets.py`, which reads them without echo. They never go into chat, a file in this repository, a log or a ticket. Run `.venv/bin/python scripts/secrets.py` at any point to see what is still unset, without decrypting anything. [infra/parameters.yaml](infra/parameters.yaml) lists every value and where it comes from.

Install the tools first: `brew install hashicorp/tap/terraform awscli gh gitleaks` and `brew install --cask session-manager-plugin`. Homebrew needs current Command Line Tools: on macOS 15, `sudo softwareupdate -i "Command Line Tools for Xcode-16.2"` is enough. Then `make setup`.

### 1. AWS

1. Sign up with an address used only for this demo, account name `release-demo`, on the **Paid plan**. The free plan blocks some services and closes after six months; credits apply on the paid plan too. If you picked the free plan, Billing, Upgrade plan, Upgrade account. Joining AWS Organizations, which step 3 does, upgrades it as well.
2. As root: IAM, Security credentials, assign MFA. Never create root access keys.
3. Set the region to eu-west-1, then IAM Identity Center, Enable (organization instance).
4. Identity Center: add a user for yourself (no groups needed), a permission set from the predefined AdministratorAccess with an 8-hour session, and assign both to the account. Copy the AWS access portal URL from Settings. Sign out of root.
5. `aws configure sso`: session name `release-demo`, your portal URL, region eu-west-1, the account and AdministratorAccess, default region eu-west-1, profile `release-demo`. From then on, `aws sso login --profile release-demo`.
6. Put your budget email in `infra/local.auto.tfvars` (git ignores it): `budget_email = "you@example.com"`.
7. `scripts/tf.sh bootstrap`, then `scripts/tf.sh apply`. The first creates the state bucket, the second the parameter names and the USD 25 budget.

### 2. GitHub

1. Create the public repository `release-demo` with nothing in it: no README, .gitignore or licence. This repository brings its own MIT licence.
2. `gh auth login`.
3. `.venv/bin/python scripts/github_app.py --owner <your username>`, open http://localhost:8765, create the app on GitHub, then install it from the printed link on `release-demo` only. It can write contents and read metadata, and has no webhook.

### 3. Slack

1. A free workspace with a neutral name, the channel `#releases`, and Sam on a second address of yours, accepted in a separate browser profile, with the full name Sam Developer.
2. api.slack.com/apps, Create New App, From a manifest: paste [slack/manifest.yml](slack/manifest.yml). Install to Workspace. In `#releases`: `/invite @Release Bot`.
3. `secrets.py set slack`. The signing secret is under Basic Information, App Credentials, Signing Secret, Show: 32 characters. Not the App-Level Token (`xapp-`) further down the page, which the demo doesn't use. The channel id, or a link to the channel, is at the bottom of the channel's About tab.
4. `secrets.py set slack` also asks for both member ids (profile, the three dots, Copy member ID). They go to SSM, not [people.yaml](people.yaml), so the public repository holds no workspace ids.

### 4. ServiceNow

1. developer.servicenow.com: sign up (the Developer Program Agreement is the checkbox on the form), then Request Instance with the release it offers. Log in to the instance directly at least once a week, or it is reclaimed.
2. New instances refuse password sign-in on the REST API ("Basic Auth Restriction") unless the user has the role `snc_basic_auth_api_access`. As admin, create a setup user: Users, New, User ID `release-demo.setup`; Set Password; Roles: `admin` and `snc_basic_auth_api_access`.
3. `secrets.py set servicenow`: the instance URL, the setup user's password (`servicenow/admin-password`), and the passwords you choose for Sam and Alex. Passwords must meet the instance's policy (upper and lower case, a digit, a special character); ServiceNow silently drops one that doesn't.
4. `.venv/bin/python scripts/servicenow_setup.py --test`. It creates Sam, Alex and `release.pipeline` (web service access only, its password generated into SSM), the groups Platform team and Change approvers, the CMDB items, and the two standard change templates. It points the Normal Change Policy at Change approvers once, at Assess, and switches off its Authorize approvals. `--test` then proves it: a normal change asks only Alex and is Scheduled after his approval, and a standard change comes out approved with no approver. Both test changes are cancelled.

### 5. Jira

1. A Jira Cloud Free site with a neutral name. Jira now calls projects spaces.
2. An empty **company-managed** Kanban space `Orders`, key `ORD`. A space made from a team-managed template comes with sample issues, and Jira never reuses keys, so delete and recreate it rather than deleting its issues.
3. Invite a third address of yours, accept it in a private window, and set the full name to Release Pipeline. Add it to ORD as Administrator, which it needs to release versions.
4. In that private window, signed in as Release Pipeline: id.atlassian.com, Security, API tokens, Create API token. A token from your own account would put your name on the pipeline's comments.
5. `secrets.py set jira` with Release Pipeline's email and token.

### 6. Grafana Cloud

1. A free stack in an EU region.
2. In the Cloud Portal, the stack's Loki card, Details: the URL and the **User** number. The stack's own id, which is also the CloudWatch External ID, is a different number.
3. Security, Access Policies: a policy with only `logs:write` on the stack, and a token for it.
4. In the stack: Administration, Users and access, Service accounts: `release-pipeline` with the Editor role, and a token.
5. Connections, Data sources, CloudWatch, Grafana Assume Role: copy the External ID. Don't save the data source until Terraform has made its role.
6. Service accounts again: `release-demo-setup` with the Admin role, and a token. Only `grafana_setup.py` on your Mac uses it, to save the CloudWatch data source; the tools host is denied it.
7. `secrets.py set grafana`.
8. Put Grafana's AWS account id (the CloudWatch data source's Grafana Assume Role instructions show it) in `infra/local.auto.tfvars` as `grafana_aws_account_id`, then `scripts/tf.sh apply`: it makes the role Grafana assumes.
9. `.venv/bin/python scripts/grafana_setup.py`: the folder, the Releases dashboard and the CloudWatch data source. Safe to run again.

### 7. SonarQube Cloud

1. Log in with GitHub and create an organization from your GitHub account, with the SonarQube Cloud app on `release-demo` only. The Free plan is enough: releases build `main`, and Free analyses `main` with the Sonar way gate. The OSS plan adds branch analysis, custom gates and webhooks.
2. Analyze new project, **Setting up a monorepo?**: one project per service. For orders-service, key `<organization>_release-demo_orders-service`, display name `orders-service`. A plain import binds the whole repository to one project and runs Automatic Analysis; monorepo projects can't, which is what the pipeline needs.
3. My Account, Security, Generate token. `secrets.py set sonar`: the organization key shows as you type, the token is hidden.

### 8. Cloudflare

The zone also serves the production website: Terraform only adds the demo's own tunnel, three `*-demo` hostnames and their Access applications, and never changes anything else in it.

1. one.dash.cloudflare.com: finish the Zero Trust onboarding on the account that holds the zone, Free plan. Settings then shows a team domain.
2. From the zone's overview page: the Account ID and the Zone ID.
3. My Profile, API Tokens, Create Custom Token, limited to that account and that zone: Account, Cloudflare Tunnel, Edit; Account, Access: Apps and Policies, Edit; Account, Access: Service Tokens, Edit; Zone, DNS, Edit.
4. `secrets.py set cloudflare`, with the email Access should let in.

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

## Demo on a call: APIs, a contract check and a canary

pricing-api is a Python Lambda function behind an API Gateway HTTP API that calls the alias `live`. Its contract is `services/pricing-api/openapi.yaml`; its fictional consumers are orders-service and the store app. Two broken versions try to ship and two different checks stop them; the third goes out. About 20 minutes with the short canary.

| Version | Jira | What changes | What catches it |
| --- | --- | --- | --- |
| 2.0.4 | | Production before the take: one national price list | |
| 2.1.0 | ORD-52 | Belgian list prices, and drops the response field `currency` | The contract check: "Contract check failed: field currency removed". No change is opened |
| 2.1.1 | ORD-52 | `currency` is back, but a Belgian store asking for a product without a Belgian list price raises an error | The canary: CloudWatch's Errors alarm on its version, its share back to 0%, the change closed as unsuccessful |
| 2.1.2 | ORD-52 | Falls back to the national price | Nothing: 10% for the set time, then all traffic |

Once, after the merge:

1. `scripts/demo.sh up apis` applies `infra/demos/4-apis` if it isn't there (Terraform shows its plan first), then waits until both APIs answer.
2. JFrog: pricing-api uses the shared repositories `demos-candidates` and `demos-releases` (made once for all demos). `scripts/jcr_setup.py --host` checks them and lists the UI steps if they are missing.
3. In SonarQube Cloud, add the monorepo project `<organization>_release-demo_pricing-api`, display name `pricing-api`, as for orders-service.
4. `.venv/bin/python scripts/servicenow_setup.py` adds the CMDB item pricing-api and its templates "Routine release of pricing-api" and "Rollback of pricing-api".
5. `.venv/bin/python scripts/demos/apis/dashboard.py` puts the dashboard "pricing-api: contract check and canary" on the Grafana stack.

Before each call or take: `scripts/reset.py apis`, which runs `scripts/demos/apis/reset.py`, in the reset job on the tools host (its JCR step needs the host): both environments on 2.0.4, production's 2.0.4 contract in `demos-releases/pricing-api/2.0.4/` for the contract check, the later release copies, tags and Jira versions cleared, and 2.0.4 staged on main. Open Grafana on that dashboard (last 15 minutes, refresh 5 seconds), Slack `#releases` as Sam, and ServiceNow's change list for pricing-api.

On the call, three times: `scripts/stage.py pricing-api <version>`, then in Slack `/release pricing-api ORD-52 call`.

- 2.1.0 stops at Contract check, before anything is published.
- 2.1.1 passes the contract check and gets the pre-approved standard change. In production it gets 10% of the traffic. Its error line climbs in Grafana at once; CloudWatch's alarm fires a minute or two later, on one-minute metrics, and its share is back at 0% within seconds. The rollback closes the change as unsuccessful, with the alarm in the close notes.
- 2.1.2 runs clean at 10%, then takes all traffic: "Live in ...".

The word `call` gives the shorter canary, `canary.minutes_on_calls` (3 minutes). Without it the canary takes `canary.minutes` (5), which the recording uses. The weight, both alarm thresholds and the load are in `services.yaml` too.

What to say if asked: the weighted alias, the alarms on the new version's `ExecutedVersion` metrics, oasdiff and JCR are all real. The traffic is generated: `scripts/demos/apis/loadgen.py` on the tools host calls the alias about 10 times a second during each canary, a third of the calls for Belgian stores, and pushes each result to Grafana. It calls the alias the way API Gateway does, because only Lambda's own answer says which version handled a call that failed. Grafana shows; CloudWatch decides.

After the call: `scripts/demo.sh down apis`. pricing-api has no server: Lambda and API Gateway charge per request, a few cents per recording, and nothing while idle. `down` only clears what an interrupted canary left, and `scripts/demo.sh status apis` shows what each environment runs.

## Demo on a call: Kubernetes (demo 8)

The catalog service on a single-node k3s cluster on the tools host, released with Helm 4 through the same pipeline: Slack, the gates, a pre-approved standard change, and a record of the rollback that Helm does by itself. It costs nothing beyond the tools host. k3s is installed there but runs only between `up` and `down`, held to 2 GB (k3s 768 MiB, its pods 1,280 MiB).

| State | Jira | What changes | Where it stops |
| --- | --- | --- | --- |
| 1.8.2 | | Production before the take | |
| 1.9.0 | ORD-90 | Search by supplier, from the catalog database named by `CATALOG_DB_URL`. Staging sets it, production doesn't | Production: never ready, Helm rolls back to 1.8.2 after 3 minutes, the check fails |
| 1.9.1 | ORD-90 | The chart gives every environment `CATALOG_DB_URL` | |

Before the call:

1. `scripts/tools.sh up`, then `scripts/demo.sh up kubernetes` (about 30 seconds; prints "Ready").
2. `.venv/bin/python scripts/demos/kubernetes/reset.py`: staging and production on 1.8.2, and 1.9.0 staged on main. Under a minute; safe to run twice.
3. For the terminal: `.venv/bin/python scripts/demos/kubernetes/connect.py` in a terminal of its own. It prints the kubeconfig to export and the prepared lines (the pods watch, `helm history`, `kubectl auth can-i ... --as $SA`). It needs `brew install --cask session-manager-plugin` once.
4. A dry run without Slack or ServiceNow: `.venv/bin/python scripts/demos/kubernetes/rehearse.py` (about 6 minutes, ends on 1.8.2). It checks every acceptance criterion and prints ok or FAIL for each.

On the call: `/release catalog ORD-90`. 1.9.0 passes staging. In production, Jenkins' log shows `helm upgrade --install ... --rollback-on-failure --wait=watcher --timeout 3m` and the helper's line for the build's 10-minute token. One 1.9.0 pod starts beside the three 1.8.2 pods and never gets ready; after 3:00 Helm rolls back. The check fails at once, the pipeline confirms the rollback, Slack says "Production check failed. Rolled back to 1.8.2.", and the change closes as unsuccessful with Helm's rollback and both image digests at the start of its close notes, and in its work notes. Then `scripts/stage.py catalog 1.9.1` and `/release catalog ORD-90` again: a second standard change, and 1.9.1 replaces the pods one at a time.

Afterwards: `scripts/demo.sh down kubernetes`. `scripts/demo.sh status kubernetes` shows the pods, their versions and the memory in use.

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
