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
| 1.1.0 | | What Terraform's first apply deploys: the page before ORD-1 | |
| 1.2.0 | ORD-1 | Shows the order count on the page | |
| 1.2.1 | ORD-2 | Rounds the API's order totals to two decimals | |
| 1.2.2 | ORD-3 | Adds the store to each order | |
| 1.2.3 | ORD-4 | Lists today's orders by order number, not newest first | |
| 1.2.4 | ORD-5 | Production reads its store list from a regional setting, `STORES`, that it doesn't have. Staging keeps the list in code, so staging never runs the new path | Production check (`/` and `/api/orders` answer 502), then rolls back to 1.2.3 |
| 1.2.5 | ORD-6 | Keeps the store list in code, as in 1.2.3 | |
| 1.2.6 | ORD-7 | Adds the status column to the orders table | |
| 1.2.7 | ORD-8 | Adds `cache-control: no-store` to the API | |
| 1.2.8 | ORD-9 | Reads the page title from a brand setting, `BRAND_TITLE`, that no environment has. The tests set it, and staging's check only asks /health | Production check (`/` answers 502), then rolls back to 1.2.7 |
| 1.2.9 | ORD-10 | Falls back to the service name when the brand setting is missing | |
| 1.3.0 | ORD-11 | Shows the environment in the page title. Production before the recording | |
| 1.4.0 | ORD-12 | Adds "Delivery estimate: 2 days" to the page. Normal change | |
| 1.4.1 | ORD-13 | Reads the estimate from the regional setting. Standard change | Production check, then rolls back to 1.4.0 |
| 1.4.0-broken-test | | /health reports the version with a "v" prefix | Tests |
| 1.4.0-broken-gate | | Untested code with a bug and code smells | Quality gate |
| 1.4.0-broken-scan | | Declares urllib3 1.22 (CVE-2018-20060, critical) | SBOM and scan |

`history.py` releases 1.2.0 to 1.3.0, the three broken 1.4.0 variants are for the acceptance checks only, and `scripts/tests/test_demo_states.py` checks that each history version passes or fails production's check as planned.

## History and reset

Both run on the tools host, because Jenkins and JFrog Container Registry answer only there (anywhere else they sit behind Cloudflare Access). From the Mac each starts itself there through SSM Run Command, as `scripts/tools.sh` does, and prints the host's log as it grows: `/var/log/release-demo-history.log` and `/var/log/release-demo-reset.log`. Ctrl-C stops the printing, not the run; `--follow` shows it again. Start the tools host first: `scripts/tools.sh up`.

```bash
.venv/bin/python scripts/history.py --dry-run    # the plan, and what is done already
.venv/bin/python scripts/history.py              # the day before recording: about 40 minutes
.venv/bin/python scripts/reset.py --dry-run      # every action reset would take
.venv/bin/python scripts/reset.py                # before each take: under two minutes
```

`history.py` releases 1.2.0 to 1.3.0 as eleven standard changes under ORD-1 to ORD-11, one at a time: it stages each version on main with `stage.py` (from a fresh clone, pushing with the GitHub App's token), starts the release job as release-bot with the parameters the Slack handler sends, as if Sam asked, and waits for it. 1.2.4 and 1.2.8 fail the production check on purpose and roll back. It ends with production on 1.3.0, and Grafana, Jira, ServiceNow, Splunk and #releases have a real past. Run it again after an interruption: it skips what ran as planned and waits for a run still going. A run that ends any other way, a failing gate or a new critical finding, stops it a day before a take.

Signed in to Jenkins as admin, the jobs `history`, `reset` and `stage` run the same scripts on the tools host, with no AWS sign-in on the Mac. Each stops at once unless admin started it. The `reset` job skips the Slack handler's warm-up, because the tools host may not look up the handler's URL; from the Mac, reset.py looks it up and passes it on.

`reset.py` puts the main demo back where `history.py` left it, and keeps the past: everything `history.py` made, and the changes, Jira comments, Slack messages and events of earlier takes, stay. It frees only what a take must create again, and archives instead of deleting. It stops release and rollback runs a take left going and cancels open orders-service changes (closed ones stay). Each tag after 1.3.0 is tagged again as `takes/<tag>-<UTC time>` on the same commit, its GitHub release moves onto that tag as the pre-release "<name> (earlier take)", and only then is the tag's own name removed. Each release copy after 1.3.0 in JFrog moves to `orders-service-releases/takes/<version>-<UTC time>/`: JCR's free edition has no move or copy API, so reset copies each file as admin, checks its SHA-256 and only then deletes the original. It un-releases the Jira versions after 1.3.0 and reopens ORD-12 and ORD-13, stages 1.3.0 on main, moves both live aliases to the newest Lambda version of 1.3.0 and calls both functions once, and warms the Slack handler with one signed request as Mateusz, who isn't a releaser, so it answers "You can't start releases here." and starts nothing. A second run finds nothing to do. `reset.py <demo>` runs `scripts/demos/<demo>/reset.py`. The scripts' own tests: `.venv/bin/python -m pytest -q scripts/tests`.

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

Before each call or take: `scripts/reset.py apis`, which runs `scripts/demos/apis/reset.py`, in the reset job on the tools host with DEMO=apis (its JCR step needs the host): both environments on 2.0.4, production's 2.0.4 contract in `demos-releases/pricing-api/2.0.4/` for the contract check, and 2.0.4 staged on main. Like the main reset it keeps the past: the later tags and their GitHub releases move under `takes/`, the later release copies under `demos-releases/pricing-api/takes/`, and the Jira versions are un-released with ORD-52 reopened. `--dry-run` shows what it would do. Open Grafana on that dashboard (last 15 minutes, refresh 5 seconds), Slack `#releases` as Sam, and ServiceNow's change list for pricing-api.

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

## Demo on a call: Linux servers in AWS and a data center (demo 3)

inventory, a Java web app on Tomcat 9, on four Ubuntu 24.04 servers: inv-cloud-01 and inv-cloud-02 on EC2 (t3.small, no inbound rules), and inv-dc-01 and inv-dc-02, Multipass virtual machines on the Mac that the cloud can't reach. Ansible deploys one server at a time (serial 1, max_fail_percentage 0): the cloud servers from the tools host through Systems Manager, the data-center servers from dc-agent, a third virtual machine whose Jenkins agent connects out over WebSocket with its own Cloudflare Access service token. Every release has its own folder under `/opt/inventory/releases` and a `current` symlink. Costs USD 0.0228 an hour per cloud server plus USD 0.005 for each public address, while they run; they stop themselves 4 hours after boot.

| State | Jira | What changes | Where it stops |
| --- | --- | --- | --- |
| 2.7.3 | | Every server before the take; built for Java 8 | |
| 2.8.0 | ORD-44 | Stock per store in the inventory API (`/api/items/<sku>`); built for Java 11, the baseline | inv-dc-01, which still runs Java 8 (planted): its check fails, its symlink goes back to 2.7.3, Tomcat restarts, the rollout stops before inv-dc-02 |

Once: `brew install tmux` and `brew install --cask session-manager-plugin` (the server panes), and Multipass (`brew install --cask multipass`). In System Settings, Privacy and Security, Local Network: allow Multipass, or `multipass exec` can't reach the machines. After the first `scripts/demo.sh up linux`: `.venv/bin/python scripts/demos/linux/jcr_token.py` (the servers' read-only JCR token; then reload Jenkins' configuration so dc-agent gets it too) and `.venv/bin/python scripts/demos/linux/servicenow_setup.py` (the four servers as CIs, after `scripts/servicenow_setup.py`).

Before the call:

1. `scripts/tools.sh up`, then `scripts/demo.sh up linux`: Terraform for the cloud servers, the three virtual machines, dc-agent connected to Jenkins, the baseline (Java 11 everywhere but inv-dc-01) and 2.7.3 on any server without a release. About 15 minutes the first time, 3 after.
2. `.venv/bin/python scripts/reset.py linux`: all four servers on 2.7.3 and Java 8 planted on inv-dc-01 again, ORD-44 reopened, open inventory changes cancelled. About a minute; safe to run twice. `--no-drift` gives inv-dc-01 Java 11, so 2.8.0 reaches all four.
3. `scripts/watch_inventory.sh`: a tmux window with the four servers' `/health` in large type, refreshed every second. `scripts/demos/linux/closeup.sh inv-dc-01` shows that server's current release, its release folders, Tomcat (`tomcat.service`) and Java. The agent's journal: `multipass exec dc-agent -- sudo journalctl -fu jenkins-agent -o cat`.

On the call: `/release inventory ORD-44`. After Alex approves, the panes switch to 2.8.0 one at a time: inv-cloud-01, inv-cloud-02, then inv-dc-01, which shows ERROR (Tomcat: built for Java 11, this server runs Java 8), switches back to 2.7.3 and restarts. Slack says "inv-dc-01 failed its check. Rolled back to 2.7.3. Rollout stopped." and "Rollout stopped at inv-dc-01 after ... inv-dc-02 untouched." The change lists the four servers as affected CIs, with each server's result in its work notes; Splunk has each server's steps.

Afterwards: `scripts/demo.sh down linux` stops the cloud servers and the virtual machines; nothing is deleted.

## Demo on a call: infrastructure changes with Terraform

An infrastructure change goes through the same gates as a release. The network stack in `infra/demos/2-terraform/network` (a VPC, two subnets and a security group, no NAT gateway and no instances) changes only through pull requests. Jenkins' multibranch job `network-stack` scans GitHub every minute and builds pull requests from this repository's own branches, never from forks. Each one gets five GitHub check runs, which the ruleset on main requires: terraform fmt, terraform validate, tflint, Checkov, and Conftest with the house rules in `infra/demos/2-terraform/checks/policy/` (required tags, "S3 bucket must not be public", allowed regions, allowed instance types). Then a plan saved to a file, with its summary and the monthly cost difference on the pull request, a normal change in ServiceNow, and after Alex approves it, the apply of exactly that file, a check that the bucket and queue exist and the bucket blocks public access, and the merge. Terraform refuses a saved plan when the state changed since the plan; the build then stops and says so.

| Commit | What changes | What happens |
| --- | --- | --- |
| 1 | ORD-22 adds `exports.tf`: an S3 bucket for store exports, made public, and an SQS queue | Checkov and "S3 bucket must not be public" fail, by name, on the pull request and in Slack. The merge is blocked |
| 2 | The bucket blocks public access; the public policy is gone | All checks pass, then "8 to add, 0 to change, 0 to destroy" and the cost difference on the pull request, the change, the apply, the merge. Slack: "Applied" |

The two commits are in `demo-states/network-stack/`. Infracost prices the bucket and queue with the expected use in `network/infracost-usage.yml` (200 GB stored, 5 million queue requests a month), so the cost shows a real number.

Once, after the merge:

1. GitHub App: in its settings, Permissions, set Checks, Pull requests and Commit statuses to Read and write, then accept the new permissions on the installation (Settings, Applications, the app, Review request).
2. `scripts/demo.sh up terraform` applies `infra/demos/2-terraform` (the plan and apply roles the tools host assumes, and the drift trigger: a CloudTrail trail, an EventBridge rule and the Lambda function `release-demo-drift-trigger`) and the network stack's baseline. Terraform shows each plan and asks first.
3. `.venv/bin/python scripts/demos/terraform/ruleset.py --apply`, with your own `gh` login: the ruleset "main: infrastructure checks". The GitHub App and repository admins may go past it, so `stage.py`, `reset.py` and your own pushes still reach main.
4. `.venv/bin/python scripts/servicenow_setup.py` adds the CMDB item network-stack.
5. Infracost: a free account and a CI token, then `.venv/bin/python scripts/secrets.py set infracost`. Without it the pull request says "not priced" and everything else runs.
6. Open a pull request once and let it fail, so the tools host builds the toolchain image (`jenkins/toolchain/terraform.Dockerfile`, a few minutes) before a take; then reset.

Before each call or take: `scripts/demos/terraform/reset.sh` (or `scripts/reset.py terraform`, once that dispatcher exists). It closes the demo's pull request, points `demo-2/store-exports` back at main, removes `exports.tf` from main with one commit, cancels open network-stack changes, resolves the drift incident, and applies main's stack, which removes the bucket, the queue and any rule added by hand. `--dry-run` shows what it would do. Open the Jenkins job `network-stack`, Slack `#releases`, and ServiceNow's change list for network-stack.

On the call:

1. `scripts/demos/terraform/open-pr.sh`: the first commit and the pull request "ORD-22 Store exports bucket and queue". The checks finish about 2 minutes after the push.
2. `scripts/demos/terraform/fix-pr.sh`: the second commit. The checks pass, and the plan and cost reach the pull request about 3 minutes after the push.
3. Approve the change in ServiceNow as Alex. Jenkins applies within 20 seconds, checks the bucket, merges, and Slack says "Applied".
4. In the AWS console, add an inbound rule to the security group `network-stack-app`. The Jenkins job `network-drift` starts by itself: CloudTrail hands the call to EventBridge, usually within a minute or two, and `release-demo-drift-trigger` queues the job with a quiet period of 60 seconds, so changes made within a minute share one run. Slack shows the rule, a line that says how many seconds after the console change the drift was found, and the incident it opened. The job also runs at 07:00 on weekdays when the tools host is up, and by hand (Build with Parameters, the parameters left empty).

Rollback is a revert: GitHub's Revert button on the merged pull request opens one, and the same flow plans, prices, approves and applies it.

What to say if asked: every tool is real, on a real AWS stack. The plan role reads only; the apply role changes only the stack's resources, and neither can change its own permissions, which live in `infra/demos/2-terraform/main.tf`, applied from the Mac. The change gets `terraform show -no-color`, which masks sensitive values; the plan file itself never leaves the build. The drift trigger leaves alone what the stack's own roles do, so an apply or a reset starts no drift run; with the tools host stopped, it logs the change and the 07:00 run finds it.

After the call: nothing to stop. The stack has nothing that runs by the hour; the bucket and queue cost cents a month until reset removes them. The drift trigger stays on: the trail's first copy of management events is free, and its bucket keeps a week of logs for cents a month. `scripts/demo.sh status terraform` shows what exists.

## Demo on a call: one release, three clouds (demo 5)

quote-service goes to AWS Lambda, Azure Container Apps and Google Cloud Run from one pipeline, as one image checked by its digest, with one change and no stored cloud keys: Jenkins signs an ID token per build (OpenID Connect Provider plugin), and each cloud trusts it. Its code is in `services/quote-service/`, its scripts in `scripts/demos/clouds/`, its Terraform in `infra/demos/5-three-clouds/` (AWS and the Jenkins issuer), `azure/` and `gcp/`.

Once, per cloud, with your own short-lived logins:

1. AWS: `scripts/demos/clouds/bootstrap.sh aws`. It makes the issuer, a public S3 bucket holding only Jenkins's two discovery files (the account's one public bucket, tagged as the exception), AWS's trust in it, ECR with 1.1.0 and both functions.
2. Azure: a pay-as-you-go subscription. `brew install azure-cli`, `az login`, `.venv/bin/python scripts/secrets.py set azure`, then `scripts/demos/clouds/bootstrap.sh azure`: the deploy identity with its federated credential and a budget alert at 10 a month.
3. Google Cloud: a project on a paid billing account. `brew install --cask gcloud-cli`, `gcloud auth login`, `gcloud auth application-default login`, `.venv/bin/python scripts/secrets.py set gcp`, then `scripts/demos/clouds/bootstrap.sh gcp`: the workload identity pool, the deploy service account and a budget alert.
4. `.venv/bin/python scripts/demos/clouds/servicenow_items.py create quote-service` for the three per-cloud CMDB items, after `servicenow_setup.py`.

Before a call:

```bash
scripts/demo.sh up clouds                          # Azure's and Google Cloud's registries and apps, on 1.1.0; waits for all six
.venv/bin/python scripts/reset.py clouds           # all six on 1.1.0, main on 1.1.0, ORD-60 reopened; or the Jenkins job reset, DEMO=clouds
.venv/bin/python scripts/stage.py quote-service 1.2.0
```

Then in Slack, as Sam: `/release quote-service ORD-60`. Show the Staging stage's log: the ID token, one line per cloud accepting it with the time its access expires, the image copied by digest from JFrog to ECR, Azure Container Registry and Artifact Registry, and the digest check with one line per registry. Alex approves in ServiceNow; production goes Google Cloud, Azure, AWS, each checked before the next. For the failure, `stage.py quote-service 1.2.1` and release again under ORD-60: Azure's production app lacks a setting 1.2.1 reads, so Google Cloud keeps 1.2.1, Azure goes back to 1.2.0 within two minutes, and AWS is never touched. The change lists every cloud and its version. Grafana's quote-service row shows the version per cloud and environment, and the response time per cloud.

After the call: `scripts/demo.sh down clouds` deletes Azure's and Google Cloud's registries and apps (the Azure registry is billed per day while it exists); their trust in Jenkins stays. `scripts/demo.sh status clouds` shows what runs. AWS costs cents at most, and nothing while idle.

If a cloud refuses the ID token with "published keys are out of date": Jenkins makes new keys at every start and configuration reload, and the tools host publishes them at boot. After a reload, on the host: `sudo /opt/release-demo-venv/bin/python scripts/demos/clouds/oidc_issuer.py publish`.

The scripts' tests: `.venv/bin/python -m pytest -q scripts/demos/clouds/tests`.

## Demo on a call: mobile apps (demo 7)

Store Floor, a Flutter app for iPhone, iPad and Android, released two ways from one pipeline. `/release store-floor ORD-80 jamf` puts 2.4.0 on the store devices through Jamf: the pilot devices first, every store after Alex approves the change. `/release store-floor ORD-81 stores` puts it in front of the testers on TestFlight and Google Play's internal track, with the release notes from the Jira version "store-floor 2.4.0". The code is in `scripts/demos/mobile/`; the app in `services/store-floor/`, with 2.3.0 and 2.4.0 in `demo-states/store-floor/`.

Jamf, the Apple Developer Program, TestFlight and Google Play run as stand-ins (`STANDIN` in the store-floor entry of `services.yaml`): they answer the same API calls, refuse what the real services refuse, and say what they are in their startup log. Jamf's is demo 6's (`scripts/demos/standins/jamf.py`, set up by `scripts/demos/desktop/jamf_setup.py`), shared by both demos; the others keep their state in demo 7's bucket. The Android signing is real: the upload key comes from SSM through Jenkins' credentials, and the log shows it as `****`.

Before a call or a take:

1. `scripts/tools.sh up`, and the Mac agent (demo 6) connected to Jenkins.
2. `scripts/demo.sh up mobile`: checks demo 7's bucket, gives the stand-ins their starting state if they have none, and starts the console on http://127.0.0.1:8077 with three pages: Device management (Store Floor's scope and the pilot devices' inventory, on demo 6's Jamf stand-in page), iOS testing and Android testing (the builds, their numbers and release notes).
3. `.venv/bin/python scripts/reset.py mobile` (it hands itself over to the tools host, like the main reset, and keeps the past the same way): every device back on 2.3.0, rebuilt with a higher build number, the scope on Pilot, 2.4.0 staged, ORD-80 and ORD-81 reopened, earlier takes archived. `--dry-run` shows what it would do. Without the Mac, `scripts/demos/mobile/reset.py --standins` resets the stand-ins alone.

During the call: Slack as Sam, the console next to it, ServiceNow as Alex. A jamf release stops at the approval with the app on the pilot devices; Alex's approval widens the scope to All store devices. A stores release is a pre-approved standard change (`standard_options` in its services.yaml entry). `/rollback store-floor` rebuilds the previous release with a higher build number and gives it to Jamf, because iOS won't install an older build over a newer one. Afterwards: `scripts/demo.sh down mobile`. Nothing of demo 7 costs money by the hour.

Setup once, after infra/ has the new parameter names:

1. `scripts/demos/mobile/tf.sh init` and `apply`: the bucket, the deploy role and two SSM values.
2. `.venv/bin/python scripts/demos/mobile/signing_setup.py android-upload-key`: the upload key into SSM. It prints the certificate's fingerprint, never the key.
3. In JFrog Container Registry's UI, the shared repositories `demos-candidates` and `demos-releases` if no other demo made them.
4. On the Mac agent: Xcode 26 or later (the Mac needs macOS 15.6 or later for it), Flutter 3.44.0 on the PATH (`brew install --cask flutter`, then `git -C /opt/homebrew/share/flutter fetch --depth 1 origin tag 3.44.0 && git -C /opt/homebrew/share/flutter checkout 3.44.0`), and `python3 -m pip install --user PyYAML` (Homebrew's Python also wants `--break-system-packages`).

For the real services, set `STANDIN` to `0` (or name only the stand-ins to keep), and:

1. Apple: the bundle id `nl.es.demo.storefloor` and the app in App Store Connect; an App Store Connect API key with the App Manager role (`signing_setup.py app-store-connect-key`, then `secrets.py set store-floor/asc-issuer-id`); `fastlane match init` with S3 storage in demo 7's bucket under `match/`, then `fastlane match adhoc` and `fastlane match appstore` once; `secrets.py set store-floor` for the match password and the team id; the internal TestFlight group "Store team" with yourself in it.
2. Google Play: the app `nl.es.demo.storefloor` in the Play Console, the first bundle uploaded there by hand to the internal track, yourself on its testers list, and the service account invited with release rights: through demo 5's federation (`store-floor/play-federation`) or a JSON key (`signing_setup.py play-service-account-key`).
3. Jamf: the in-house app "Store Floor", the mobile device groups Pilot and All store devices, automatic updates on in In-House App Maintenance, and an API client allowed to read and update mobile device apps, read mobile devices and groups, and send mobile device commands (`secrets.py set store-floor` for its URL, id and secret). A supervised iPad or iPhone enrolled in Pilot, its UDID in the ad hoc profile.

## Demo on a call: desktop apps (demo 6)

Store Assistant is a small SwiftUI app for the Mac and its WPF twin for Windows: its version in large type and today's orders from orders-service. One run builds both. The Mac track builds, signs from a keychain made for that build only, packages, notarizes and staples on the Mac agent; the Windows track builds, signs and verifies the MSI on the Windows build agent. The tools host then sends the Mac package through Jamf to the Pilot group, installs the MSI on the Windows test instance, and after Alex approves the rollout change widens the Jamf policy to All managed Macs.

| Version | Jira | What it is |
| --- | --- | --- |
| 1.6.0 | ORD-50 | The baseline: today's order count and the opening hours. Released once, as a standard change |
| 1.7.0 | ORD-71 | Today's orders as a list, with a planted bug: the list stays empty while the count says 12. The pilot catches it, Alex rejects the rollout, `/rollback store-assistant` puts 1.6.0 back |
| 1.7.1 | ORD-71 | The fix, for calls: approved, the scope widens to every managed Mac |

Apple (Developer ID, notarization, Gatekeeper), Microsoft's Artifact Signing and Jamf run as stand-ins (`STANDIN: "1"` in services.yaml; `STANDIN_APPLE`, `STANDIN_JAMF` and `STANDIN_MICROSOFT` switch one). They say so in their startup log only. The app is really signed, with hardened runtime and Apple's secure timestamp, by an identity made for the build; productbuild only signs with an installer identity Apple issued, so the package itself is unsigned until the real certificates are in. The notary stand-in really inspects the package and rejects what Apple would. The MSI is signed and timestamped with the Windows build agent's own test certificate, which the agent and the test instance trust. The Jamf stand-in is a Lambda function (`scripts/demos/standins/jamf.py`) that answers the same Jamf Pro and Classic API calls; its page shows each policy's package and scope and its groups' inventory.

Once, after the merge:

1. `scripts/demo.sh up desktop` applies `infra/demos/6-desktop` if needed (Terraform shows its plan first), starts the Windows build agent and test instance, and sets up Jamf's groups, the policy Store Assistant and five store Macs on 1.6.0 (`scripts/demos/desktop/jamf_setup.py`).
2. The Mac agent: install a Java runtime (`brew install --cask temurin@21`) and Xcode 26, then `scripts/demos/desktop/mac_agent_setup.sh`. The Windows build agent connects by itself once its Cloudflare token and Jenkins secret are in SSM.
3. The pilot Mac: a macOS virtual machine on your Mac, never your work Mac. With Tart: `brew install cirruslabs/cli/tart`, `tart clone ghcr.io/cirruslabs/macos-sequoia-base:latest pilot`, `tart run pilot`, then `scripts/demos/desktop/pilot_setup.sh admin@$(tart ip pilot)` (its password is admin). It names it pilot-mac-01, points Store Assistant at orders-service and enrolls it.
4. JFrog: store-assistant uses the shared `demos-candidates` and `demos-releases`. In SonarQube Cloud add `<organization>_release-demo_store-assistant`. `.venv/bin/python scripts/servicenow_setup.py` adds the CMDB item and the templates "Routine release of", "Rollback of store-assistant" and "Pilot of Store Assistant".
5. The baseline: `scripts/stage.py store-assistant 1.6.0`, `/release store-assistant ORD-50 standard`, and `sudo jamf policy` on the pilot Mac when the run waits for it.

Before each call or take: `scripts/reset.py desktop --pilot-ssh admin@<pilot Mac>`. Jamf's policy back on 1.6.0 and Pilot, the pilot Mac and the store Macs on 1.6.0, the test instance on 1.6.0, open changes cancelled, the take's tags and release copies archived, ORD-71 reopened, and 1.7.0 staged on main. Open Store Assistant on the pilot Mac, the Jamf stand-in's page (`.venv/bin/python scripts/demos/standins/jamf.py ui-link --only "Store Assistant"` opens it with a signed link, valid 12 hours), Slack `#releases` as Sam, and ServiceNow as Alex.

On the call: `/release store-assistant ORD-71`. Notarization takes about two and a half minutes. When the run waits at Check staging, run `sudo jamf policy` on the pilot Mac: 1.7.0 arrives and the app reopens with its empty list. Alex rejects the rollout change in ServiceNow: the run stops, the change is cancelled and Slack says 1.7.0 stays on the Pilot group. `/rollback store-assistant`, then `sudo jamf policy` again: 1.6.0 is back. For the happy path, stage 1.7.1 and approve.

What to say if asked: the builds, the app signature, the keychain, the checks, JFrog, ServiceNow and the Windows install are real; Apple's notary service, Microsoft's identity validation and Jamf are stand-ins with the same interfaces, because their accounts and trials take days or weeks. The Mac updates only when it checks in: every 15 minutes, or at once with `sudo jamf policy`.

After the call: `scripts/demo.sh down desktop` stops the Windows instances (USD 0.064 an hour each while they run; they also stop themselves 4 hours after boot). The Jamf stand-in costs nothing idle. `scripts/demo.sh status desktop` shows the instances, the Mac agent and the policy.

## Demo on a call: Windows store servers (demo 1)

store-sync, a .NET Framework 4.8 Windows service, upgraded from 3.2.0 to 3.3.0 (ORD-31) on six store servers, STORE-001 to STORE-006, in waves: STORE-001 alone, then two at a time. Every step runs through AWS Systems Manager Run Command; no server has an inbound rule, RDP or WinRM. STORE-004's config lacks UploadEndpoint, planted on purpose: its check fails, it goes back to 3.2.0 by itself, the wave stops, and the change closes as "Successful with issues". The fleet costs about USD 0.41 an hour while it runs, and each server stops itself 4 hours after boot.

Once, before the first take:

1. In JCR's UI, the two shared Generic repositories `demos-candidates` and `demos-releases` (`scripts/jcr_setup.py --host` lists the steps).
2. `.venv/bin/python scripts/servicenow_setup.py`, then `.venv/bin/python scripts/demos/windows/servicenow_fleet.py`: store-sync's CMDB item and rollback template, and the six servers as Windows Server CIs that run it.
3. `.venv/bin/python scripts/demos/windows/seed.py`: builds 3.2.0 with Docker and puts it in JCR, checked against the SHA-256 that `services.yaml` pins. `up` also runs it when the baseline is missing.

For each call or take:

```bash
scripts/tools.sh up                    # the tools host first: the fleet downloads from its JCR
scripts/demo.sh up windows             # under 15 minutes from nothing, about 4 from stopped
scripts/demo.sh status windows         # what each server runs, the drift, the cost
.venv/bin/python scripts/demos/windows/reset.py     # before every retake: all six on 3.2.0, the drift planted
```

1. Grafana, dashboard Store fleet (folder Release demo): the wave progress and one tile per server, refreshed every 10 seconds.
2. Slack, as Sam: `/release store-sync ORD-31`. The change lists the six servers as affected items and the waves in its implementation plan. Approve it as Alex.
3. Systems Manager, Run Command, Command history: every step, named in its comment (`store-sync 3.3.0: install on STORE-002 (CHG...)`).
4. STORE-004 fails its check and rolls back; Slack says "STORE-004 failed its check, back on 3.2.0. Wave stopped." STORE-005 finishes; STORE-006 is never touched. Each server has a work note in the change.
5. `scripts/demos/windows/fix-drift.sh` restores UploadEndpoint. Then `/release store-sync ORD-31 resume`: a new change, linked to the first, for STORE-004 and STORE-006 only.

`scripts/demo.sh down windows` stops the fleet; only its disks cost, about USD 0.52 a day. `scripts/demos/windows/down.sh --destroy` removes it with Terraform.

## Demo on a call: Claude's notes, checked by script (videos 12 and 13)

Two notes Claude writes for people, each checked by a script before anyone sees it, and neither in the release's path: the ai-notes job starts with `wait: false` and `propagate: false`, and nothing waits for it.

- On a normal change, a work note by the ServiceNow user AI Notes: what changes, the four results with their sources, and "Not checked by the pipeline", where `scripts/evidence.py` names each setting the new code requires that an environment doesn't define. `scripts/demos/ai_notes/verify.py` withholds the whole summary if one number, setting name, file or version isn't in the evidence. evidence.json is attached either way.
- After a failed production check, once the rollback is recorded everywhere, the likely cause in the release message's Slack thread, as a work note and as a Jira comment: up to three quotes from the failed version's CloudWatch lines, the diff and the settings, each kept only if it matches a line of its source word for word. It never suggests a fix.

Once, after the merge: the evidence reader role (`scripts/demos/ai_notes/tf.sh apply`, IAM, so you apply it), `scripts/tf.sh apply` for the new parameter names and the tools host's statements, `scripts/secrets.py set anthropic/api-key` if it isn't set, and `.venv/bin/python scripts/servicenow_setup.py` for the user ai.notes. The tools host installs the SDK from `requirements-dev.txt` at its next boot, and Jenkins makes the job ai-notes at its next configuration reload.

Before each call or take: `scripts/reset.py ai-notes`: production on 1.4.0 from a real release, 1.4.1 staged, the switch on. Then `/release orders-service ORD-13` for the summary and Alex's rejection, and `/release orders-service ORD-13 standard` for the rollback and the likely cause.

After it: `scripts/demos/ai_notes/switch.py off` (the main reset does it too). Off, nothing new runs anywhere. Each call's tokens and cost go to Splunk and Grafana Logs, priced from `scripts/demos/ai_notes/prices.yaml`; a summary costs about USD 0.13, a likely cause about USD 0.18.

## Demo on a call: a cost check in staging (video 18)

orders-service 1.7.0 asks Claude for a delivery message per order on every page view; 1.7.1 asks once per order with a smaller model and keeps the messages in DynamoDB. With the cost check on, the stage Cost check after Check staging sends 50 page views to staging, reads what each used from Lambda's REPORT lines and the function's own lines, prices it with `prices.yaml` and projects the month at the expected volume in `services.yaml`. Above the limit, the change becomes a normal one that only Cost approvers decide: Fiona Finance. It uses no model, and never stops or approves a release itself.

Once, after the merge: `scripts/demo.sh up cost-gate` (the tables, each function's policy and the 30-second timeout; you apply, plans shown first), `scripts/secrets.py set servicenow/fiona-password`, then `.venv/bin/python scripts/demos/cost_gate/setup.py`, which makes the Jira issue and versions and runs `servicenow_setup.py --test`, and `.venv/bin/python scripts/grafana_setup.py` for the row "orders-service cost".

Before each call or take: re-read the pricing pages and update `prices.yaml`'s date, `scripts/demo.sh check cost-gate`, then `scripts/reset.py cost-gate`: production on 1.4.0 from a real release with its cost check, 1.7.0 staged, the switch on. A 1.7.0 take costs about USD 1 in Claude calls.

After it: `scripts/demo.sh down cost-gate` turns the switch off; the tables stay, at cents a month.

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
