// The auditor's sample (video 11): draws SAMPLE_SIZE of orders-service's
// production changes in the period, with SEED, and answers five questions for
// each from the original records. It only reads: ServiceNow as evidence.reader,
// JFrog as evidence-reader, AWS through the role release-demo-evidence-reader.
// Anyone with Build on this job may start it. The parameters reach the shell
// as environment variables, never quoted into the script.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 10, unit: 'MINUTES')
    }
    stages {
        stage('Sample') {
            steps {
                script {
                    if (!(params.SEED ?: '').trim()) {
                        error 'SEED is required: any whole number. The same seed and period draw the same changes.'
                    }
                }
                sh '''
                    rm -rf evidence-out
                    python3 scripts/demos/auditor/evidence_sample.py --from "$FROM" --to "$TO" \\
                        --size "$SAMPLE_SIZE" --seed "$SEED" --out evidence-out
                '''
                script {
                    currentBuild.description = readFile('evidence-out/description.txt').trim()
                }
            }
        }
    }
    post {
        always {
            dir('evidence-out') {
                archiveArtifacts artifacts: '**', allowEmptyArchive: true
            }
        }
    }
}
