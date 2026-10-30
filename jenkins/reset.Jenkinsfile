// reset.py from the Jenkins UI, before a take: puts a demo back to its
// starting state. The demo's name reaches the shell as an environment
// variable, never quoted into the script. Only admin may start it.
// DEMO=clouds runs with quote-service's ID tokens bound (demo 5).

@Library('release-demo') _

pipeline {
    agent none
    options {
        timeout(time: 10, unit: 'MINUTES')
    }
    stages {
        stage('Who asked') {
            steps {
                script {
                    def started = currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause')
                    if (!started || started[0].userId != 'admin') {
                        error "Only admin runs ${env.JOB_NAME}: it moves tags, GitHub releases and release copies."
                    }
                }
            }
        }
        stage('Reset') {
            agent { label 'tools-host' }
            steps {
                script {
                    // OPTIONS, the demo's own (demo 20: --checks on --stage 1.7.0), splits into
                    // words unquoted; set -f keeps the shell from expanding any of them.
                    String reset = "set -f; python3 scripts/reset.py \"\$DEMO\" --here${params.DRY_RUN ? ' --dry-run' : ''} \$OPTIONS"
                    if (params.DEMO == 'clouds') {
                        withServiceCredentials(serviceConfig('quote-service')) { sh reset }
                    } else {
                        sh reset
                    }
                }
            }
        }
    }
}
