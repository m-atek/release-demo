// stage.py from the Jenkins UI: puts a demo state on main, for a call. It
// runs from a fresh clone and pushes as the GitHub App (scripts/host.py).
// Only admin may start it.

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
                        error "Only admin runs ${env.JOB_NAME}: it pushes to main."
                    }
                }
            }
        }
        stage('Stage') {
            agent { label 'tools-host' }
            steps {
                sh 'python3 scripts/host.py stage "$SERVICE" "$VERSION"'
            }
        }
    }
}
