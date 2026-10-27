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

// Views next to All: a release dashboard with the latest builds and test
// trends, and a monitor with one big tile per job for a second screen.
List<String> demoJobs = ['release', 'rollback', 'ci', 'tools-smoke']

dashboardView('Releases') {
    description('Releases and CI runs on the demo platform. Demo system, not a client\'s.')
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

// Demo 2: every pull request on the network stack, and the morning drift
// check. GitHub can't reach Jenkins, so the job scans every minute. It builds
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
                    repositoryUrl("https://github.com/${repository}")
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

pipelineJob('network-drift') {
    description('Demo 2: does the network stack in AWS still match main? Weekdays at 07:00, when the tools host is up, or by hand. Drift goes to Slack and opens a ServiceNow incident.')
    properties {
        disableConcurrentBuilds()
        pipelineTriggers { triggers { cron { spec('TZ=Europe/Amsterdam\n0 7 * * 1-5') } } }
    }
    logRotator { numToKeep(60) }
    definition fromRepository('infra/demos/2-terraform/drift.Jenkinsfile')
}
