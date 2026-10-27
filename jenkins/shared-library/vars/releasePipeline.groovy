// The release pipeline every service shares: the 12 stages from build to
// record. Each demo adds only its own deploy, check and rollback steps; the
// stage logic lives in scripts/. Started by the Slack handler or history.py,
// as the Jenkins user release-bot, with these parameters:
// SERVICE, JIRA_KEY, OPTION, REQUESTED_BY, SLACK_CHANNEL, SLACK_TS.

def call() {
    node('tools-host') {
        def checkedOut = checkout scm
        env.GIT_COMMIT = checkedOut.GIT_COMMIT
        String service = params.SERVICE
        sh """
            rm -rf .release dist
            python3 scripts/run_state.py init --service '${service}' --jira-key '${params.JIRA_KEY}' \\
                --option '${params.OPTION ?: ''}' --requested-by '${params.REQUESTED_BY ?: ''}' \\
                --slack-channel '${params.SLACK_CHANNEL ?: ''}' --slack-ts '${params.SLACK_TS ?: ''}'
        """
        String version = state('version')
        currentBuild.description = "${service} ${version} for ${params.JIRA_KEY}, asked by ${params.REQUESTED_BY ?: 'the Jenkins UI'}"
        sh 'python3 scripts/jira.py fix-version'

        try {
            releaseStage('Build') { toolchain.build(service, params.JIRA_KEY) }
            releaseStage('Test') { toolchain.test(service) }
            releaseStage('Quality gate') { toolchain.qualityGate(service, version) }
            releaseStage('SBOM and scan') { toolchain.sbomAndScan(service) }
            archiveArtifacts artifacts: '.release/reports/*, dist/**/build-info.json', allowEmptyArchive: true
            releaseStage('Publish') { sh "python3 scripts/publish.py ${service}" }
            releaseStage('Staging', environment: 'staging') { sh "python3 scripts/deploy.py ${service} staging" }
            releaseStage('Check staging', environment: 'staging') {
                if (check(service, 'staging', version) != 0) {
                    sh "python3 scripts/deploy.py ${service} staging --back"
                    error "Staging check failed: staging is back on ${state('deploys.staging.previous_version')}."
                }
            }
            releaseStage('Change') { sh 'python3 scripts/change.py open' }
            if (state('change.type') == 'standard') {
                releaseStage.skip('Approval', 'A standard change is pre-approved.')
            } else {
                releaseStage('Approval') {
                    sh 'python3 scripts/change.py wait'
                    sh "python3 scripts/notify.py line 'Approved by ${state('change.approved_by')} in ServiceNow.'"
                }
            }
            releaseStage('Production', environment: 'production', deploy: true) {
                sh 'python3 scripts/change.py implement'
                sh "python3 scripts/deploy.py ${service} production"
            }
            boolean live = true
            releaseStage('Check production', environment: 'production') {
                if (check(service, 'production', version) != 0) {
                    live = false
                    rollBack(service)
                    error "Production check failed: rolled back to ${state('deploys.production.previous_version')}."
                }
            }
            releaseStage('Record') {
                sh """
                    python3 scripts/publish.py ${service} --release
                    python3 scripts/notify.py finish live
                    python3 scripts/jira.py comment "Released ${service} ${version} in \$(python3 scripts/run_state.py get change.number). Live in \$(python3 scripts/run_state.py get total). Build: ${env.BUILD_URL}"
                    python3 scripts/jira.py release
                    python3 scripts/tag.py
                    python3 scripts/events.py annotate "${service} ${version} released (${params.JIRA_KEY})" --tags release,${service}
                    python3 scripts/change.py close successful --notes "Released ${service} ${version}. Total time: \$(python3 scripts/run_state.py get total). Build: ${env.BUILD_URL}"
                """
            }
        } catch (error) {
            stopped(service, error)
            throw error
        }
    }
}

// The pipeline's check: /health within the environment's timeout, then the check paths.
int check(String service, String environment, String version) {
    String url = state("deploys.${environment}.url")
    return sh(returnStatus: true, script: "python3 scripts/check.py ${service} ${environment} --version ${version} --url '${url}'")
}

// After a failed production check: the previous version back, checked, and recorded everywhere.
void rollBack(String service) {
    long started = System.currentTimeMillis()
    String previous = state('deploys.production.previous_version')
    sh "python3 scripts/deploy.py ${service} production --back"
    if (check(service, 'production', previous) != 0) {
        sh "python3 scripts/notify.py line 'The previous version, ${previous}, fails its check too. Look now.'"
        error "Rolled back to ${previous}, but it fails its check too."
    }
    String recovery = String.format('%.0f', (System.currentTimeMillis() - started) / 1000.0)
    sh """
        python3 scripts/events.py --stage Rollback --result rolled-back --environment production --recovery-seconds ${recovery}
        python3 scripts/events.py annotate "${service} rolled back to ${previous}" --tags rollback,${service}
        python3 scripts/notify.py finish rolled-back --previous ${previous}
        python3 scripts/change.py close unsuccessful --notes "Production check failed. The pipeline rolled back to ${previous} automatically in ${recovery}s and checked it. Total time: \$(python3 scripts/run_state.py get total). Build: ${env.BUILD_URL}"
        python3 scripts/jira.py comment "Release of ${service} \$(python3 scripts/run_state.py get version) failed its production check and was rolled back to ${previous} in ${recovery}s. Change: \$(python3 scripts/run_state.py get change.number). Build: ${env.BUILD_URL}"
    """
}

// Any other failure: Slack says where it stopped, and an open change is cancelled.
void stopped(String service, error) {
    if (state('total')) {
        return  // the rollback already told everyone
    }
    sh(returnStatus: true, script: "python3 scripts/notify.py finish failed")
    sh(returnStatus: true, script: "python3 scripts/change.py cancel --notes 'The release stopped: ${error.message?.replace("'", '')}. Nothing changed in production.'")
}

String state(String key) {
    return sh(returnStdout: true, script: "python3 scripts/run_state.py get ${key}").trim()
}
