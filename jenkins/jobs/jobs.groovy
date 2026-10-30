// Every Jenkins job, as Job DSL, loaded by casc.yaml. The repository is public,
// so Jenkins reads it without credentials; pushes go through the GitHub App.

String repository = new File('/run/secrets/github-repository').with { it.exists() ? it.text.trim() : 'm-atek/release-demo' }
String repositoryUrl = "https://github.com/${repository}.git"

// A closure, not a method: it needs repositoryUrl from this script.
def fromRepository = { String path ->
    return {
        cpsScm {
            scm { git { remote { url(repositoryUrl) }; branch('*/main') } }
            scriptPath(path)
            lightweight(true)
        }
    }
}

pipelineJob('tools-smoke') {
    description('Checks the tools host: the agent, a pinned toolchain container, JFrog Container Registry and Splunk.')
    definition {
        cps {
            sandbox(true)
            script(new File('/var/jenkins_casc/jobs/tools-smoke.Jenkinsfile').text)
        }
    }
}

pipelineJob('release') {
    description('Releases a service from build to production. Started by the Slack handler or history.py as release-bot.')
    parameters {
        stringParam('SERVICE', 'orders-service', 'A service from services.yaml.')
        stringParam('JIRA_KEY', '', 'The Jira issue the release is for, such as ORD-12.')
        stringParam('OPTION', '', 'standard for a pre-approved standard change.')
        stringParam('REQUESTED_BY', '', 'Slack user id of who asked.')
        stringParam('SLACK_CHANNEL', '', 'Channel of the Slack status message.')
        stringParam('SLACK_TS', '', 'Timestamp of the Slack status message.')
        stringParam('REQUESTED_AT', '', 'Epoch seconds of the Slack command, where the total time starts.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(200) }
    definition fromRepository('Jenkinsfile')
}

pipelineJob('rollback') {
    description('Rolls production back to the release before the live one, and records it. Started by /rollback in Slack as release-bot.')
    parameters {
        stringParam('SERVICE', 'orders-service', 'A service from services.yaml.')
        stringParam('REQUESTED_BY', '', 'Slack user id of who asked.')
        stringParam('SLACK_CHANNEL', '', 'Channel of the Slack status message.')
        stringParam('SLACK_TS', '', 'Timestamp of the Slack status message.')
        stringParam('REQUESTED_AT', '', 'Epoch seconds of the Slack command, where the total time starts.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(200) }
    definition fromRepository('jenkins/rollback.Jenkinsfile')
}

pipelineJob('ci') {
    description('Every push to main: build, test, quality gate, SBOM and scan. Polls every 5 minutes; skips [skip ci].')
    properties {
        disableConcurrentBuilds()
        pipelineTriggers { triggers { pollSCM { scmpoll_spec('H/5 * * * *') } } }
    }
    logRotator { numToKeep(100) }
    definition fromRepository('jenkins/ci.Jenkinsfile')
}

// Mateusz's jobs: the scripts he runs from the Mac, from the Jenkins UI. They
// push to main and move tags, releases and release copies, so each
// Jenkinsfile stops at once unless admin started it: under Matrix-based
// security release-bot, whose token the Slack handler holds, may start every job.
pipelineJob('history') {
    description('history.py: releases orders-service 1.2.0 to 1.3.0 under ORD-1 to ORD-11 as standard changes, the day before recording. About 40 minutes.')
    parameters {
        booleanParam('DRY_RUN', true, 'Print the plan and what is done already; change nothing.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(20) }
    definition fromRepository('jenkins/history.Jenkinsfile')
}

pipelineJob('reset') {
    description('reset.py: puts a demo back to its starting state before a take, keeping the past. main: orders-service 1.3.0 everywhere, earlier takes archived under takes/.')
    parameters {
        stringParam('DEMO', 'main', 'main, or a folder in scripts/demos/.')
        booleanParam('DRY_RUN', false, 'Print every action; change nothing.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(50) }
    definition fromRepository('jenkins/reset.Jenkinsfile')
}

pipelineJob('stage') {
    description('stage.py: puts a demo state on main with one commit, pushed as the GitHub App.')
    parameters {
        stringParam('SERVICE', 'orders-service', 'A service from services.yaml.')
        stringParam('VERSION', '1.3.0', 'A folder in demo-states/<service>/.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(50) }
    definition fromRepository('jenkins/stage.Jenkinsfile')
}

// Video 10: rotate.py from the Jenkins UI, like history, reset and stage.
pipelineJob('rotate') {
    description('rotate.py: revokes one of the platform\'s secrets, gives the pipeline a new one, proves the old one dead and records an incident. Video 10.')
    parameters {
        choiceParam('SECRET', ['jcr', 'sonar'], 'jcr: the pipeline\'s JFrog token. sonar: the quality gate\'s SonarQube Cloud token.')
        stringParam('REASON', '', 'Why, for the incident, such as: pasted in Slack.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(50) }
    definition fromRepository('jenkins/rotate.Jenkinsfile')
}

// Video 11: the auditor's sample. ines, the auditor, may see and start this
// job and no other (casc.yaml: Overall/Read only). It only reads. The period
// defaults to the last 7 days as of the last configuration reload.
String today = java.time.LocalDate.now(java.time.ZoneId.of('Europe/Amsterdam')).toString()
String weekAgo = java.time.LocalDate.now(java.time.ZoneId.of('Europe/Amsterdam')).minusDays(7).toString()
pipelineJob('evidence-sample') {
    description('Draws a sample of orders-service\'s production changes and answers five questions for each from the original records: ServiceNow, JFrog and AWS. No AI. The same seed and period give the same findings hash.')
    parameters {
        stringParam('FROM', weekAgo, 'First day of the period, yyyy-mm-dd, Amsterdam time.')
        stringParam('TO', today, 'Last day of the period, yyyy-mm-dd.')
        stringParam('SAMPLE_SIZE', '10', 'How many changes to draw.')
        stringParam('SEED', '', 'Any whole number. Anyone can draw the same sample again with it.')
    }
    properties {
        authorizationMatrix {
            inheritanceStrategy { inheriting() }
            entries {
                user {
                    name('ines')
                    permissions(['Job/Build', 'Job/Read'])
                }
            }
        }
    }
    logRotator { numToKeep(100) }
    definition fromRepository('jenkins/evidence-sample.Jenkinsfile')
}

// Video 16: the coding agent on a Jira issue, started by /agent in Slack as
// release-bot; and the four checks every pull request to main gets, from the
// pipeline's own GitHub App, while demo 16's rulesets are on.
pipelineJob('agent-task') {
    description('The coding agent, Claude Code, on one Jira issue, with its own GitHub App and AWS role. Started by /agent in Slack. Video 16.')
    parameters {
        stringParam('SERVICE', 'orders-service', 'A service with coding_agent in services.yaml.')
        stringParam('JIRA_KEY', '', 'The Jira issue to work on.')
        stringParam('NOTE', '', 'What the requester asked, at most 300 characters.')
        stringParam('REQUESTED_BY', '', 'Slack user id of who asked.')
        stringParam('SLACK_CHANNEL', '', 'Channel of the Slack status message.')
        stringParam('SLACK_TS', '', 'Timestamp of the Slack status message.')
        stringParam('REQUESTED_AT', '', 'Epoch seconds of the Slack command, where the total time starts.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(200) }
    definition fromRepository('jenkins/agent-task.Jenkinsfile')
}

pipelineJob('pull-requests') {
    description('Every minute: the newest open pull request whose head has no checks from the pipeline\'s app gets Build, Test, Quality gate and SBOM and scan, from main\'s definitions. Never forks. Only while demo 16\'s rulesets are on. Video 16.')
    properties {
        disableConcurrentBuilds()
        pipelineTriggers { triggers { cron { spec('* * * * *') } } }
    }
    logRotator { numToKeep(300) }
    definition fromRepository('jenkins/pull-requests.Jenkinsfile')
}

// Demo 3: the data-center servers' part of an inventory rollout, on dc-agent.
// The release on the tools host starts it as release-bot and waits for it.
pipelineJob('inventory-dc-wave') {
    description('Demo 3: deploys, checks and rolls back inventory on inv-dc-01 and inv-dc-02, from dc-agent. Started by the release.')
    parameters {
        stringParam('REQUEST', '', 'What to run, as JSON. Written by scripts/demos/linux/jenkins_job.py.')
    }
    properties { disableConcurrentBuilds() }
    logRotator { numToKeep(100) }
    definition fromRepository('jenkins/jobs/inventory-dc-wave.Jenkinsfile')
}

// Demo 7: builds and signs Store Floor, iOS on the Mac and Android on the
// tools host in parallel. Started by store-floor's build, rollback and reset scripts.
pipelineJob('store-floor-build') {
    description('Builds and signs Store Floor for iOS (Mac agent) and Android (tools host) in parallel. Started as release-bot by scripts/demos/mobile.')
    parameters {
        stringParam('VERSION', '', 'The version in services/store-floor/VERSION of the code built.')
        stringParam('STORE_BUILD', '', 'The store build number: the release build number, or a higher one such as 57.1 for a rebuild.')
        stringParam('SOURCE', '', 'A commit, or demo-state:<version>.')
        stringParam('PLATFORMS', 'ios,android', 'ios, android, or both.')
        stringParam('ORDERS_URL', '', "orders-service's production URL, for the app's order list.")
        stringParam('REQUESTED_BY', '', 'The run that asked for the build.')
    }
    logRotator { numToKeep(30) }
    definition fromRepository('scripts/demos/mobile/build.Jenkinsfile')
}

// Videos 12 and 13: the change summary and the likely cause, written by
// Claude and checked by script. The release starts it with wait: false and
// propagate: false, while the AI notes switch is on; runs may overlap.
pipelineJob('ai-notes') {
    description('Videos 12 and 13: a summary on a normal change, or the likely cause after a rollback, checked by script. Started by the release; it never changes the release.')
    parameters {
        stringParam('KIND', 'summary', 'summary (a normal change) or cause (after a rollback).')
        stringParam('SERVICE', 'orders-service', 'A service with ai_notes in services.yaml.')
        stringParam('RELEASE_BUILD', '', 'The release build whose archive holds evidence.json.')
        stringParam('CHANGE_SYS_ID', '', "The change's sys_id.")
        stringParam('CHANGE_NUMBER', '', 'The change, such as CHG0030050.')
        stringParam('JIRA_KEY', '', 'The Jira issue of the release.')
        stringParam('SLACK_CHANNEL', '', 'Channel of the Slack status message.')
        stringParam('SLACK_TS', '', 'Timestamp of the Slack status message, for the thread reply.')
        stringParam('FAILED_LAMBDA_VERSION', '', 'cause: the Lambda version that failed the production check.')
        stringParam('ROLLED_BACK_TO', '', 'cause: the version production is back on.')
        stringParam('DEPLOYED_AT', '', 'cause: epoch seconds of the failed deploy.')
        stringParam('ROLLED_BACK_AT', '', 'cause: epoch seconds of the rollback.')
    }
    logRotator { numToKeep(200) }
    definition fromRepository('jenkins/ai-notes.Jenkinsfile')
}

// Views next to All: a release dashboard with the latest builds and test
// trends, and a monitor with one big tile per job for a second screen.
List<String> demoJobs = ['release', 'rollback', 'ci', 'tools-smoke']

dashboardView('Releases') {
    description('Releases and CI runs on the demo platform.')
    jobs { names(*demoJobs) }
    columns {
        status()
        weather()
        name()
        lastSuccess()
        lastFailure()
        lastDuration()
        buildButton()
    }
    topPortlets {
        latestBuilds {
            name('Latest builds')
            numBuilds(15)
        }
    }
    leftPortlets {
        buildStatistics { displayName('Build results') }
    }
    rightPortlets {
        testTrendChart { displayName('Test trend') }
    }
}

buildMonitorView('Monitor') {
    description('One tile per job: green, red or running.')
    jobs { names(*demoJobs) }
}

// Demo 2: every pull request on the network stack, and the drift check.
// GitHub can't reach Jenkins, so the job scans every minute. It builds
// pull requests from branches in this repository only, never from forks. The
// GitHub App credential lets the GitHub Checks plugin publish each check run;
// the plugin's own overall check and GitHub Branch Source's commit statuses
// are off, so the pull request lists exactly the checks the ruleset requires.
String repositoryOwner = repository.tokenize('/')[0]
String repositoryName = repository.tokenize('/')[1]

multibranchPipelineJob('network-stack') {
    displayName('network-stack pull requests')
    description('Demo 2: for every pull request, the checks, a saved plan with its cost, a ServiceNow change, and after approval the apply of exactly that plan and the merge. Scans GitHub every minute; never builds forks.')
    branchSources {
        branchSource {
            source {
                github {
                    id('network-stack')
                    repoOwner(repositoryOwner)
                    delegate.repository(repositoryName)
                    // delegate: the script has its own repositoryUrl variable.
                    delegate.repositoryUrl("https://github.com/${repository}")
                    configuredByUrl(false)
                    credentialsId('github-app')
                    traits {
                        gitHubPullRequestDiscovery { strategyId(1) }
                        gitHubStatusChecks {
                            skip(true)
                            skipNotifications(true)
                            suppressLogs(true)
                        }
                    }
                }
            }
        }
    }
    factory {
        workflowBranchProjectFactory { scriptPath('infra/demos/2-terraform/Jenkinsfile') }
    }
    triggers {
        periodicFolderTrigger { interval('1m') }
    }
    orphanedItemStrategy {
        discardOldItems { numToKeep(20) }
    }
}

// The drift check runs weekdays at 07:00, by hand, and a minute or two after a
// change made outside Terraform: the Lambda function release-demo-drift-trigger
// starts it as release-bot then, with the change as its parameters and a quiet
// period of 60 seconds. Runs by hand leave the parameters empty.
pipelineJob('network-drift') {
    description('Demo 2: does the network stack in AWS still match main? A minute or two after a change made outside Terraform, weekdays at 07:00 when the tools host is up, or by hand. Drift goes to Slack and opens a ServiceNow incident.')
    parameters {
        stringParam('TRIGGER', '', 'event when a change in AWS started this run. Empty for the 07:00 run and runs by hand.')
        stringParam('EVENT_NAME', '', 'The call that changed the stack, such as AuthorizeSecurityGroupIngress.')
        stringParam('EVENT_TIME', '', "The call's time in CloudTrail, in UTC, such as 2026-10-29T16:54:24Z.")
        stringParam('RESOURCE_ID', '', 'What the call changed: a security group id or a bucket name.')
        booleanParam('FROM_CONSOLE', false, 'The call came from the AWS console.')
    }
    properties {
        disableConcurrentBuilds()
        pipelineTriggers { triggers { cron { spec('TZ=Europe/Amsterdam\n0 7 * * 1-5') } } }
    }
    logRotator { numToKeep(60) }
    definition fromRepository('infra/demos/2-terraform/drift.Jenkinsfile')
}
