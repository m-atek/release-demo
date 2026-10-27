// history.py from the Jenkins UI, the day before recording: releases
// orders-service 1.2.0 to 1.3.0 as eleven standard changes, one at a time,
// and waits for each. It holds one executor; each release run takes another.
// Only admin may start it: release-bot, whose token the Slack handler holds,
// may start every job under Matrix-based security.

pipeline {
    agent none
    options {
        timeout(time: 3, unit: 'HOURS')
    }
    stages {
        stage('Who asked') {
            steps {
                script {
                    def started = currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause')
                    if (!started || started[0].userId != 'admin') {
                        error "Only admin runs ${env.JOB_NAME}: it pushes to main and starts releases."
                    }
                }
            }
        }
        stage('History') {
            agent { label 'tools-host' }
            steps {
                sh "python3 scripts/history.py --here${params.DRY_RUN ? ' --dry-run' : ''}"
            }
        }
    }
}
