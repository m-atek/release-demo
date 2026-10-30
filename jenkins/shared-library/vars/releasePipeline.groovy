// The release pipeline every service shares: the 12 stages from build to
// record. Each demo adds only its own deploy, check and rollback steps; the
// stage logic lives in scripts/. Started by the Slack handler or history.py,
// as the Jenkins user release-bot, with these parameters:
// SERVICE, JIRA_KEY, OPTION, REQUESTED_BY, SLACK_CHANNEL, SLACK_TS, REQUESTED_AT.
//
// Values from outside the pipeline (parameters, versions, names, error
// messages) reach the shell as environment variables, never quoted into the
// script: a value with an apostrophe can't break or change a command.

def call() {
    node('tools-host') {
        def checkedOut = checkout scm
        env.GIT_COMMIT = checkedOut.GIT_COMMIT
        String service = params.SERVICE
        sh '''
            rm -rf .release dist
            python3 scripts/run_state.py init --service "$SERVICE" --jira-key "$JIRA_KEY" \\
                --option "$OPTION" --requested-by "$REQUESTED_BY" \\
                --slack-channel "$SLACK_CHANNEL" --slack-ts "$SLACK_TS" --requested-at "${REQUESTED_AT:-}"
        '''
        String version = state('version')
        Map cfg = serviceConfig(service)
        String deploy = serviceConfig.command(cfg, 'deploy')
        String publish = serviceConfig.command(cfg, 'publish')
        currentBuild.displayName = "#${env.BUILD_NUMBER} ${service} ${version}"
        currentBuild.description = "${params.JIRA_KEY}${params.OPTION ? ', ' + params.OPTION : ''}, asked by ${params.REQUESTED_BY ? 'Slack' : 'the Jenkins UI'}"
        sh 'python3 scripts/jira.py fix-version'

        try {
            releaseStage('Build') { toolchain.build(service, params.JIRA_KEY, cfg) }
            releaseStage('Test') { toolchain.test(service, cfg) }
            releaseStage('Quality gate') { toolchain.qualityGate(service, version, cfg) }
            releaseStage('SBOM and scan') { toolchain.sbomAndScan(service) }
            if (cfg.scripts.contract) {
                releaseStage('Contract check') { sh "${serviceConfig.command(cfg, 'contract')} ${service}" }
            }
            archiveArtifacts artifacts: '.release/reports/*, dist/**/build-info.json', allowEmptyArchive: true
            releaseStage('Publish') { sh "${publish} ${service}" }
            if (cfg.environments?.containsKey('staging')) {
                releaseStage('Staging', environment: 'staging') {
                    withServiceCredentials(cfg) { sh "${deploy} ${service} staging" }
                }
                releaseStage('Check staging', environment: 'staging') {
                    if (check(cfg, service, 'staging', version) != 0) {
                        withServiceCredentials(cfg) { sh "${deploy} ${service} staging --back" }
                        error "Staging check failed: staging is back on ${state('deploys.staging.previous_version')}."
                    }
                }
                if (cfg.scripts.evaluation) {
                    // Demo 23: fixed questions against what staging now runs, before any change exists.
                    // A failure leaves staging on this version, so the team can ask it what went wrong.
                    releaseStage('Evaluation', environment: 'staging') {
                        sh "${serviceConfig.command(cfg, 'evaluation')} ${service}"
                    }
                }
            } else {
                // Production only (demos 1 and 3): the deploy script is never called for staging.
                releaseStage.skip('Staging', "${service} has no staging environment.")
                releaseStage.skip('Check staging', "${service} has no staging environment.")
            }
            if (cfg.scripts.plan) {
                // What the change needs from the target before it opens (demo 1: the wave plan).
                releaseStage('Plan') { sh "${serviceConfig.command(cfg, 'plan')} ${service}" }
            }
            releaseStage('Change') { sh 'python3 scripts/change.py open' }
            if (state('change.type') == 'standard') {
                releaseStage.skip('Approval', 'A standard change is pre-approved.')
            } else {
                releaseStage('Approval') {
                    sh 'python3 scripts/change.py wait'
                    withEnv(["APPROVER=${state('change.approved_by')}"]) {
                        sh 'python3 scripts/notify.py line "Approved by $APPROVER in ServiceNow."'
                    }
                }
            }
            releaseStage('Production', environment: 'production', deploy: true) {
                sh 'python3 scripts/change.py implement'
                withServiceCredentials(cfg) { sh "${deploy} ${service} production" }
            }
            releaseStage('Check production', environment: 'production') {
                if (check(cfg, service, 'production', version) != 0) {
                    rollBack(cfg, service)
                    error "Production check failed: rolled back to ${state('deploys.production.previous_version')}."
                }
            }
            releaseStage('Record') {
                withEnv(["VERSION=${version}", "PUBLISH=${publish}"]) {
                    sh '''
                        $PUBLISH "$SERVICE" --release
                        python3 scripts/notify.py finish live
                        python3 scripts/jira.py comment "Released $SERVICE $VERSION in $(python3 scripts/run_state.py get change.number). Live in $(python3 scripts/run_state.py get total). Build: $BUILD_URL"
                        python3 scripts/jira.py release
                        python3 scripts/tag.py
                        python3 scripts/events.py annotate "$SERVICE $VERSION released ($JIRA_KEY)" --tags "release,$SERVICE"
                        python3 scripts/change.py close successful --notes "Released $SERVICE $VERSION. Total time: $(python3 scripts/run_state.py get total). Build: $BUILD_URL"
                    '''
                }
            }
        } catch (error) {
            stopped(error)
            throw error
        }
    }
}

// The pipeline's check: /health within the environment's timeout, then the check paths.
int check(Map cfg, String service, String environment, String version) {
    String url = state("deploys.${environment}.url")
    return withServiceCredentials(cfg) {
        withEnv(["CHECK_VERSION=${version}", "CHECK_URL=${url}"]) {
            sh(returnStatus: true,
                script: "${serviceConfig.command(cfg, 'check')} ${service} ${environment} --version \"\$CHECK_VERSION\" --url \"\$CHECK_URL\"")
        }
    }
}

// After a failed production check: the previous version back, checked, and recorded everywhere.
void rollBack(Map cfg, String service) {
    long started = System.currentTimeMillis()
    String previous = state('deploys.production.previous_version')
    withServiceCredentials(cfg) { sh "${serviceConfig.command(cfg, 'deploy')} ${service} production --back" }
    withEnv(["PREVIOUS=${previous}"]) {
        if (check(cfg, service, 'production', previous) != 0) {
            sh 'python3 scripts/notify.py line "The previous version, $PREVIOUS, fails its check too. Look now."'
            error "Rolled back to ${previous}, but it fails its check too."
        }
        String recovery = String.format('%.0f', (System.currentTimeMillis() - started) / 1000.0)
        withEnv(["RECOVERY=${recovery}"]) {
            sh '''
                CHECK_NOTES=$(python3 scripts/run_state.py get check.notes)
                python3 scripts/events.py --stage Rollback --result rolled-back --environment production --recovery-seconds "$RECOVERY"
                python3 scripts/events.py annotate "$SERVICE rolled back to $PREVIOUS" --tags "rollback,$SERVICE"
                python3 scripts/notify.py finish rolled-back --previous "$PREVIOUS"
                python3 scripts/change.py close unsuccessful --notes "${CHECK_NOTES:+$CHECK_NOTES }Production check failed. The pipeline rolled back to $PREVIOUS automatically in ${RECOVERY}s and checked it. Total time: $(python3 scripts/run_state.py get total). Build: $BUILD_URL"
                python3 scripts/jira.py comment "Release of $SERVICE $(python3 scripts/run_state.py get version) failed its production check and was rolled back to $PREVIOUS in ${RECOVERY}s. Change: $(python3 scripts/run_state.py get change.number). Build: $BUILD_URL"
            '''
        }
    }
}

// Any other failure: Slack says where it stopped. Before production, the open
// change is cancelled; after the production deploy, it closes unsuccessful,
// unless the deploy script closed it already (change.closed, demo 1's stop rule).
void stopped(error) {
    if (state('total')) {
        return  // the rollback already told everyone
    }
    // The notes add their own full stop after the reason, so one it ends with goes:
    // "fails its check too." would read "too.. Look at production now."
    String reason = (error.message ?: error.toString()).readLines().first().replaceAll(/\.+$/, '')
    withEnv(["REASON=${reason}"]) {
        sh(returnStatus: true, script: 'python3 scripts/notify.py finish failed')
        if (state('change.closed')) {
            echo "The change is closed already: ${state('change.closed')}."
        } else if (state('deploys.production.number')) {
            sh(returnStatus: true, script: 'python3 scripts/change.py close unsuccessful --notes "The release stopped after the production deploy: $REASON. Look at production now."')
        } else {
            sh(returnStatus: true, script: 'python3 scripts/change.py cancel --notes "The release stopped: $REASON. Nothing changed in production."')
        }
    }
}

String state(String key) {
    return sh(returnStdout: true, script: "python3 scripts/run_state.py get ${key}").trim()
}
