// orders-service-tests (demo 15): Build, Test, and SBOM and scan of
// orders-service at a demo state, staged into this workspace only and never
// committed, with the per-test results and an event per stage. Test reruns a
// failed test once (scripts/tests_run.py): it is unstable on a flaky run and
// fails on a real failure. flaky-night runs it 80 times from one commit of main.
@Library('release-demo') _

node('tools-host') {
    checkout scm
    if (params.COMMIT) {
        sh 'git checkout --quiet --detach "$COMMIT"'
    }
    env.GIT_COMMIT = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
    sh '''
        rm -rf .release dist
        python3 scripts/stage.py orders-service "$VERSION" --no-commit
        python3 scripts/run_state.py init --service orders-service --jira-key "" --requested-by orders-service-tests
    '''
    currentBuild.displayName = "#${env.BUILD_NUMBER} orders-service ${params.VERSION} tests"
    currentBuild.description = "Commit ${env.GIT_COMMIT.substring(0, 7)}${params.COMMIT ? ', from flaky-night' : ''}"
    if (params.COMMIT) {
        // flaky-night's runs time a test on this host: each waits until no other
        // build keeps its CPUs busy, as when calibrate.py measured the limit.
        sh 'python3 scripts/demos/flaky/quiet.py'
    }
    Map cfg = serviceConfig('orders-service')
    testsStage('Build') { toolchain.build('orders-service', 'CI', cfg) }
    testsStage('Test') {
        toolchain.test('orders-service', cfg, 'tests')
        if (fileExists('.release/reports/unstable.txt')) {
            unstable(readFile('.release/reports/unstable.txt').trim())
        }
    }
    testsStage('SBOM and scan') { toolchain.sbomAndScan('orders-service') }
}

// A stage, its seconds in the run state (the per-test summary reads Build's) and
// its event to Splunk and Grafana, marked job tests so release views leave it out.
void testsStage(String name, Closure body) {
    stage(name) {
        long started = System.currentTimeMillis()
        String result = 'failed'
        try {
            body()
            result = 'ok'
        } finally {
            String seconds = String.format('%.1f', (System.currentTimeMillis() - started) / 1000.0)
            sh "python3 scripts/run_state.py set 'stages.${name}.status=${result}' 'stages.${name}.seconds=${seconds}'"
            sh(returnStatus: true,
               script: "python3 scripts/events.py --stage '${name}' --result ${result} --seconds ${seconds} --job tests")
        }
    }
}
