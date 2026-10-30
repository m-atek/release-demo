// rotate.py from the Jenkins UI: revokes and replaces one of the platform's
// secrets and proves the old one dead (video 10). The secret and the reason
// reach the shell as environment variables, never quoted into the script.
// Only admin may start it: it revokes the token every release publishes with.

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
                        error "Only admin runs ${env.JOB_NAME}: it revokes the tokens the pipeline uses."
                    }
                }
            }
        }
        stage('Rotate') {
            agent { label 'tools-host' }
            steps {
                sh 'python3 scripts/rotate.py "$SECRET" --reason "$REASON" --here'
            }
        }
    }
}
