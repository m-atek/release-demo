// reset.py from the Jenkins UI, before a take: puts a demo back to its
// starting state. The demo's name reaches the shell as an environment
// variable, never quoted into the script. Only admin may start it.

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
                sh "python3 scripts/reset.py \"\$DEMO\" --here${params.DRY_RUN ? ' --dry-run' : ''}"
            }
        }
    }
}
