// patch-clock-seed (video 24): puts orders-service 1.12.0 on both aliases, outside
// the pipeline, because the gate refuses it. Staged into this workspace only and
// never committed, built as the pipeline builds it, with Syft alone over the zip:
// no Grype, no run state, no change, no Slack, no event. Only admin may start it,
// like reset and stage. scripts/demos/patch_clock/seed.py says the rest.
@Library('release-demo') _

node('tools-host') {
    def started = currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause')
    if (!started || started[0].userId != 'admin') {
        error "Only admin runs ${env.JOB_NAME}: it moves both live aliases, outside the pipeline."
    }
    checkout scm
    env.GIT_COMMIT = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
    sh '''
        rm -rf .release dist
        python3 scripts/stage.py orders-service 1.12.0 --no-commit
    '''
    currentBuild.displayName = "#${env.BUILD_NUMBER} orders-service 1.12.0 seed"
    Map cfg = serviceConfig('orders-service')
    stage('Build') { toolchain.build('orders-service', 'seed', cfg) }
    stage('Seed') { sh 'python3 scripts/demos/patch_clock/seed.py' }
}
