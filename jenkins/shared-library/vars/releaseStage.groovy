// One release stage. Before it runs, the Slack message shows it running. When
// it ends, ok or failed, the time goes to the Slack message, an event goes to
// Splunk and Grafana, and once the change exists, a work note goes to it.
// A failure is recorded the same way, then rethrown.
//
//   releaseStage('Staging', environment: 'staging') { sh '...' }

def call(Map options = [:], String name, Closure body) {
    stage(name) {
        long started = System.currentTimeMillis()
        sh "python3 scripts/notify.py stage '${name}' running"
        try {
            body()
        } catch (error) {
            finish(name, 'failed', started, options)
            throw error
        }
        finish(name, 'ok', started, options)
    }
}

def skip(String name, String reason) {
    stage(name) {
        sh "python3 scripts/notify.py stage '${name}' skipped"
        sh "python3 scripts/events.py --stage '${name}' --result skipped"
        sh "python3 scripts/change.py note '${name}: skipped. ${reason}'"
    }
}

private void finish(String name, String result, long started, Map options) {
    String seconds = String.format('%.1f', (System.currentTimeMillis() - started) / 1000.0)
    String extra = options.environment ? " --environment ${options.environment}" : ''
    extra += options.deploy && result == 'ok' ? ' --deploy' : ''
    sh "python3 scripts/notify.py stage '${name}' ${result} --seconds ${seconds}"
    sh "python3 scripts/events.py --stage '${name}' --result ${result} --seconds ${seconds}${extra}"
    sh "python3 scripts/change.py note '${name}: ${result} in ${seconds}s'"
}
