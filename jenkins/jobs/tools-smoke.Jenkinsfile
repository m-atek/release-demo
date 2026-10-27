// Checks the tools host end to end: the agent is connected, it can run a
// pinned toolchain container, and it reaches JFrog Container Registry and
// Splunk's HTTP Event Collector. Run it after scripts/tools.sh up.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 10, unit: 'MINUTES')
    }
    stages {
        stage('Toolchain container') {
            steps {
                script {
                    docker.image('python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530').inside {
                        sh 'python --version'
                    }
                }
            }
        }
        stage('JFrog Container Registry') {
            steps {
                sh 'curl -sf "$JCR_URL/artifactory/api/system/ping"'
            }
        }
        stage('Splunk') {
            steps {
                withCredentials([string(credentialsId: 'splunk-hec-token', variable: 'HEC_TOKEN')]) {
                    sh '''
                        printf '{"sourcetype": "_json", "event": {"check": "tools-smoke", "build": "%s"}}' "$BUILD_TAG" > event.json
                        curl -sf "$SPLUNK_HEC_URL/services/collector/event" -H "Authorization: Splunk $HEC_TOKEN" --data @event.json
                    '''
                }
            }
        }
    }
}
