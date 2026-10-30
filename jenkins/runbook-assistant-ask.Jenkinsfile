// /ask in Slack (demo 23): production's runbook assistant answers in the
// message the Slack handler posted, with its sources and its version. The
// question reaches the shell as an environment variable, never quoted into
// the script. On the tools-host agent itself, with the host's Python
// environment: the answer path reads its pointer and keys from SSM with the
// instance role, which a toolchain container can't reach.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 3, unit: 'MINUTES')
    }
    stages {
        stage('Answer') {
            steps {
                sh 'python3 scripts/demos/rag/ask.py'
            }
        }
    }
}
