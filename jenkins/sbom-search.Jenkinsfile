// /sbom in Slack (video 24): which live service holds a CVE, in the message the
// Slack handler posted. The id reaches the shell as an environment variable,
// never quoted into the script. On the tools-host agent itself: it reads SSM
// and assumes release-demo-sbom-reader with the instance role, which a toolchain
// container can't reach, and runs the pinned Grype in Docker. It reads and
// reports. Several may run at once.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 5, unit: 'MINUTES')
    }
    stages {
        stage('Search') {
            steps {
                sh 'python3 scripts/demos/patch_clock/search.py'
            }
        }
    }
}
