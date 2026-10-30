// flaky-night (demo 15), the night before recording: runs orders-service-tests
// RUNS times in a row, PAUSE_SECONDS apart, all from the commit main has when
// it starts: 80 runs take about 4 hours. Its runs keep the tools host awake,
// and the idle stop powers it off 30 minutes after the last. Only admin may
// start it, like history and reset: release-bot may start every job.

pipeline {
    agent none
    options {
        timeout(time: 8, unit: 'HOURS')
    }
    stages {
        stage('Who asked') {
            steps {
                script {
                    def started = currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause')
                    if (!started || started[0].userId != 'admin') {
                        error "Only admin runs ${env.JOB_NAME}: it holds the tools host for hours."
                    }
                }
            }
        }
        stage('Night') {
            steps {
                script {
                    String commit = ''
                    node('tools-host') {
                        checkout scm
                        commit = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
                    }
                    int runs = Integer.parseInt(params.RUNS)
                    int pause = Integer.parseInt(params.PAUSE_SECONDS)
                    currentBuild.description = "${runs} runs of orders-service ${params.VERSION} at ${commit.substring(0, 7)}"
                    Map results = [:]
                    for (int run = 1; run <= runs; run++) {
                        def tests = build(job: 'orders-service-tests', wait: true, propagate: false, parameters: [
                            string(name: 'VERSION', value: params.VERSION), string(name: 'COMMIT', value: commit)])
                        results[tests.result] = (results[tests.result] ?: 0) + 1
                        echo "Run ${run} of ${runs}: #${tests.number} ${tests.result}"
                        if (run < runs) {
                            sleep(time: pause, unit: 'SECONDS')
                        }
                    }
                    echo "Done: ${results}"
                }
            }
        }
    }
}
