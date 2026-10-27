// The rollback /rollback <service> starts: production back to the release
// before the live one, checked, recorded as a pre-approved change from the
// service's rollback template, and reported in Slack, on the rolled-back
// release's Jira issue, and as events. Started by the Slack handler as the
// Jenkins user release-bot, with these parameters:
// SERVICE, REQUESTED_BY, SLACK_CHANNEL, SLACK_TS, REQUESTED_AT.
//
// Production moves first and is recorded after, so the recovery never waits
// for ServiceNow or Jira. As in releasePipeline, values from outside reach the
// shell as environment variables, never quoted into the script.

def call() {
    node('tools-host') {
        def checkedOut = checkout scm
        env.GIT_COMMIT = checkedOut.GIT_COMMIT
        String service = params.SERVICE
        Map cfg = serviceConfig(service)
        String deploy = serviceConfig.command(cfg, 'deploy')
        sh '''
            rm -rf .release dist
            python3 scripts/run_state.py init --service "$SERVICE" --jira-key "" --requested-by "$REQUESTED_BY" \\
                --slack-channel "$SLACK_CHANNEL" --slack-ts "$SLACK_TS" --requested-at "${REQUESTED_AT:-}"
            python3 scripts/rollback.py init
        '''
        currentBuild.displayName = "#${env.BUILD_NUMBER} ${service}"
        currentBuild.description = "Rollback of production, asked by ${params.REQUESTED_BY ? 'Slack' : 'the Jenkins UI'}"

        long started = System.currentTimeMillis()
        try {
            rollbackStep('Roll back', 'production') {
                // The deploy script's last error line is what Slack shows, such as "no earlier release".
                int status = sh(returnStatus: true, script: "${deploy} ${service} production --previous 2>.release/move-error.txt")
                String why = readFile('.release/move-error.txt').trim()
                if (why) {
                    echo why
                }
                if (status != 0) {
                    error(why ? why.readLines().last() : "${deploy} exited with ${status}")
                }
                sh 'python3 scripts/rollback.py moved'
            }
        } catch (error) {
            finish('failed', "the rollback didn't run: ${firstLine(error)}")
            throw error
        }
        String was = state('version')
        String back = state('rolled_back_to')
        currentBuild.displayName = "#${env.BUILD_NUMBER} ${service} ${was} to ${back}"

        boolean healthy = false
        try {
            rollbackStep('Check rollback', 'production') {
                healthy = check(cfg, service, back) == 0
                if (!healthy) {
                    error "${back} fails the production check too"
                }
            }
        } catch (error) {
            // Production changed either way: the change below records it as unsuccessful.
        }
        String recovery = String.format('%.0f', (System.currentTimeMillis() - started) / 1000.0)

        withEnv(["WAS=${was}", "BACK=${back}", "RECOVERY=${recovery}"]) {
            try {
                rollbackStep('Record rollback', null) {
                    sh 'python3 scripts/rollback.py find-issue'
                    sh(healthy ? '''
                        python3 scripts/change.py rollback successful --notes "Rolled back $SERVICE in production from $WAS to $BACK with /rollback, and checked it, in ${RECOVERY}s. Build: $BUILD_URL"
                        python3 scripts/events.py --stage Rollback --result rolled-back --environment production --recovery-seconds "$RECOVERY"
                        python3 scripts/events.py annotate "$SERVICE rolled back to $BACK" --tags "rollback,$SERVICE"
                        if [ -n "$(python3 scripts/run_state.py get jira_key)" ]; then
                            python3 scripts/jira.py comment "$SERVICE $WAS was rolled back in production to $BACK with /rollback, in ${RECOVERY}s. Change: $(python3 scripts/run_state.py get change.number). Build: $BUILD_URL"
                        fi
                    ''' : '''
                        python3 scripts/change.py rollback unsuccessful --notes "Rolled back $SERVICE in production from $WAS to $BACK with /rollback, but $BACK fails the production check too. Look at production now. Build: $BUILD_URL"
                        if [ -n "$(python3 scripts/run_state.py get jira_key)" ]; then
                            python3 scripts/jira.py comment "$SERVICE $WAS was rolled back in production to $BACK with /rollback, but $BACK fails the production check too. Change: $(python3 scripts/run_state.py get change.number). Build: $BUILD_URL"
                        fi
                    ''')
                }
            } catch (error) {
                finish('failed', healthy ? 'recording it failed: the build log says why' : "${back} fails the production check too. Look at production now")
                throw error
            }
        }

        if (!healthy) {
            finish('failed', "${back} fails the production check too. Look at production now")
            error "Rolled back to ${back}, but it fails its check too."
        }
        finish('rolled-back', '')
    }
}

// One rollback step. The Slack message shows it running, then its result and
// time; an event goes to Splunk and Grafana either way. A failure is rethrown.
void rollbackStep(String name, String environment, Closure body) {
    stage(name) {
        long started = System.currentTimeMillis()
        sh "python3 scripts/rollback.py stage '${name}' running"
        try {
            body()
        } catch (error) {
            stepDone(name, 'failed', started, environment)
            throw error
        }
        stepDone(name, 'ok', started, environment)
    }
}

private void stepDone(String name, String result, long started, String environment) {
    String seconds = String.format('%.1f', (System.currentTimeMillis() - started) / 1000.0)
    String extra = environment ? " --environment ${environment}" : ''
    sh "python3 scripts/rollback.py stage '${name}' ${result} --seconds ${seconds}"
    sh "python3 scripts/events.py --stage '${name}' --result ${result} --seconds ${seconds}${extra}"
}

// The production check, on the version the rollback put back.
int check(Map cfg, String service, String version) {
    String url = state('deploys.production.url')
    return withEnv(["CHECK_VERSION=${version}", "CHECK_URL=${url}"]) {
        sh(returnStatus: true,
            script: "${serviceConfig.command(cfg, 'check')} ${service} production --version \"\$CHECK_VERSION\" --url \"\$CHECK_URL\"")
    }
}

// The last line of the Slack message. Never fails the build on its own.
void finish(String result, String reason) {
    withEnv(["RESULT=${result}", "REASON=${reason}"]) {
        sh(returnStatus: true, script: 'python3 scripts/rollback.py finish "$RESULT" --reason "$REASON"')
    }
}

String firstLine(error) {
    return (error.message ?: error.toString()).readLines().first()
}

String state(String key) {
    return sh(returnStdout: true, script: "python3 scripts/run_state.py get ${key}").trim()
}
