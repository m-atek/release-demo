// The drift check for the network stack: does AWS still match main? Runs at
// 07:00 on weekdays, when the tools host is up, by hand, and a minute or two
// after a change made outside Terraform: the Lambda function
// release-demo-drift-trigger (scripts/demos/terraform/drift_trigger.py) starts
// it with TRIGGER=event and the change's call, time and resource, which
// drift.py report reads from the build's environment. When terraform plan
// -detailed-exitcode exits with 2, the differences go to Slack and an incident
// opens in ServiceNow. Run by the job network-drift.

node('tools-host') {
    checkout scm
    def demo = load 'infra/demos/2-terraform/pipeline.groovy'
    boolean timer = !currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()
    String trigger = params.TRIGGER == 'event' ? 'event' : (timer ? 'timer' : 'manual')
    if (trigger == 'event') {
        currentBuild.description = "After ${params.EVENT_NAME} on ${params.RESOURCE_ID}"
    }
    withEnv(["DRIFT_TRIGGER=${trigger}"]) {
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
