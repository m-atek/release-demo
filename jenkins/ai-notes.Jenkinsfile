// Videos 12 and 13: a normal change's summary (KIND summary) and the likely
// cause after a rollback (KIND cause), written by Claude and checked by
// script. The release starts this job with wait: false and propagate: false,
// so nothing waits for it and its result never reaches the release. The
// parameters reach the script as environment variables, never quoted into it.
// See scripts/demos/ai_notes/job.py.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 3, unit: 'MINUTES')
    }
    stages {
        stage('AI notes') {
            steps {
                script {
                    currentBuild.displayName = "#${env.BUILD_NUMBER} ${params.KIND} for release #${params.RELEASE_BUILD}"
                    currentBuild.description = "${params.SERVICE} ${params.CHANGE_NUMBER}"
                }
                sh 'python3 scripts/demos/ai_notes/job.py'
            }
        }
    }
}
