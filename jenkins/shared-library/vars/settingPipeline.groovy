// One value in a service's settings file, through AWS AppConfig's validator,
// staging and production: what /setting starts (video 29). Nothing builds and
// nothing publishes. Started by the Slack handler as the Jenkins user release-bot,
// with these parameters: SERVICE, SETTING, VALUE, REQUESTED_BY, SLACK_CHANNEL,
// SLACK_TS, REQUESTED_AT.
//
// The rows, events and work notes are releaseStage's. As in releasePipeline,
// values from outside the pipeline reach the shell and Python as environment
// variables, never quoted into a script. Events carry the service as
// orders-service-settings, so a setting doesn't count as a release on any panel.

def call() {
    node('tools-host') {
        def checkedOut = checkout scm
        env.GIT_COMMIT = checkedOut.GIT_COMMIT
        sh '''
            rm -rf .release dist
            python3 scripts/run_state.py init --service "$SERVICE" --jira-key "" --requested-by "$REQUESTED_BY" \\
                --slack-channel "$SLACK_CHANNEL" --slack-ts "$SLACK_TS" --requested-at "${REQUESTED_AT:-}"
            python3 scripts/demos/settings/setting.py init
        '''
        currentBuild.displayName = "#${env.BUILD_NUMBER} ${params.SERVICE} ${params.SETTING}"
        currentBuild.description = "Asked by ${params.REQUESTED_BY ? 'Slack' : 'the Jenkins UI'}"
        String step = 'python3 scripts/demos/settings/setting.py'

        try {
            releaseStage('AWS validator') { sh "${step} validate" }
            releaseStage('Staging', environment: 'staging') { sh "${step} deploy staging" }
            releaseStage('Check staging', environment: 'staging') {
                if (sh(returnStatus: true, script: "${step} check staging") != 0) {
                    sh "${step} back staging"
                    error 'Staging check failed'
                }
            }
            releaseStage('Change') { sh "${step} change" }
            releaseStage.skip('Approval', 'A standard change is pre-approved.')
            releaseStage('Production', environment: 'production') {
                sh 'python3 scripts/change.py implement'
                sh "${step} deploy production"
            }
            releaseStage('Check production', environment: 'production') {
                if (sh(returnStatus: true, script: "${step} check production") != 0) {
                    error 'AWS AppConfig put the last value back'
                }
            }
            releaseStage('Record') {
                sh 'python3 scripts/notify.py finish live'
                sh "${step} close"
            }
        } catch (error) {
            stopped(error)
            throw error
        }
    }
}

// Slack says where it stopped. Before production a change, if one is open, is
// cancelled; after the production deployment it closes unsuccessful.
void stopped(error) {
    if (state('total')) {
        return
    }
    String reason = (error.message ?: error.toString()).readLines().first().replaceAll(/\.+$/, '')
    withEnv(["REASON=${reason}"]) {
        sh(returnStatus: true, script: 'python3 scripts/notify.py finish failed')
        if (state('deploys.production.number')) {
            sh(returnStatus: true, script: 'python3 scripts/change.py close unsuccessful --notes "The setting stopped after the production deployment: $REASON."')
        } else {
            sh(returnStatus: true, script: 'python3 scripts/change.py cancel --notes "The setting stopped: $REASON. Nothing changed in production."')
        }
    }
}

String state(String key) {
    return sh(returnStdout: true, script: "python3 scripts/run_state.py get ${key}").trim()
}
