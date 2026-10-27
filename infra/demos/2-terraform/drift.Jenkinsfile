// The drift check for the network stack: does AWS still match main? Runs at
// 07:00 on weekdays, when the tools host is up, or by hand. When terraform
// plan -detailed-exitcode exits with 2, the differences go to Slack and an
// incident opens in ServiceNow. Run by the job network-drift.

node('tools-host') {
    checkout scm
    def demo = load 'infra/demos/2-terraform/pipeline.groovy'
    boolean timer = !currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()
    withEnv(["DRIFT_TRIGGER=${timer ? 'timer' : 'manual'}"]) {
        sh 'rm -rf .release'
        try {
            stage('Plan') {
                sh 'python3 scripts/demos/terraform/stack.py backend'
                sh 'python3 scripts/demos/terraform/stack.py credentials plan'
                demo.image().inside('-e HOME=/tmp') {
                    sh 'python3 scripts/demos/terraform/drift.py plan'
                }
            }
            stage('Report') {
                sh 'python3 scripts/demos/terraform/drift.py report'
            }
        } finally {
            sh 'rm -rf .release/aws'
        }
    }
}
