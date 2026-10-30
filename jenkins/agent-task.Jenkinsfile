// The coding agent works on one Jira issue (video 16): Claude Code in the
// coding-agent container, on its own GitHub App and its own AWS role, neither
// of which may ship. Started by /agent in Slack as release-bot, with SERVICE,
// JIRA_KEY, NOTE, REQUESTED_BY, SLACK_CHANNEL, SLACK_TS and REQUESTED_AT.
// The work is scripts/demos/agent_ship/agent_task.py; its tokens stay in that
// process and never reach this log. Values reach the shell as environment
// variables, never quoted into the script.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 25, unit: 'MINUTES')
        disableConcurrentBuilds()
    }
    stages {
        stage('Coding agent') {
            steps {
                script {
                    currentBuild.displayName = "#${env.BUILD_NUMBER} agent-task ${params.JIRA_KEY}"
                    currentBuild.description = "${params.SERVICE}: ${params.NOTE ?: ''}".take(200)
                }
                sh 'rm -rf agent-work .release'
                sh(label: 'Coding agent', script: 'python3 scripts/demos/agent_ship/agent_task.py run')
            }
        }
    }
    post {
        always {
            script {
                // gitleaks' default rules over the whole log, as a last check: the
                // formatter masked every value already, so this should find nothing.
                int found = sh(returnStatus: true, script: '''
                    [ -f .release/agent-log.txt ] || exit 0
                    docker run --rm -v "$PWD/.release:/scan:ro" \\
                        zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f \\
                        dir /scan --no-banner --redact
                ''')
                if (found != 0) {
                    currentBuild.result = 'FAILURE'
                    echo 'gitleaks found a secret in the agent\'s log: see above. The log stays on this build only.'
                }
            }
            // The clone: the agent's files are its own, written with a wide umask so this can remove them.
            sh 'rm -rf agent-work || true'
        }
    }
}
