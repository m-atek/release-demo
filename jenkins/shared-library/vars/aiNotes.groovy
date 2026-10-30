// The AI notes of videos 12 and 13, kept out of the release's path. Nothing
// here waits for a model or can fail a release: every step returns or only
// prints on an error, and the ai-notes job starts with wait: false and
// propagate: false. With the switch off (scripts/demos/ai_notes/switch.py),
// or for a service without ai_notes in services.yaml, none of it runs.
//
//   env.AI_NOTES = aiNotes.on() ? 'on' : 'off'      once, after the run state exists
//   aiNotes.beforeChange()                          evidence.json, in the Change stage
//   aiNotes.afterChange(cfg)                        its change, archived; the summary for a normal change
//   aiNotes.cause(previous, rolledBackAt)           after a rollback is recorded everywhere

boolean on() {
    try {
        return sh(returnStdout: true, script: 'python3 scripts/demos/ai_notes/switch.py is-on "$SERVICE"').trim() == 'on'
    } catch (error) {
        echo "AI notes: off, the switch couldn't be read (${error.message})"
        return false
    }
}

void beforeChange() {
    int status = sh(returnStatus: true, script: 'python3 scripts/evidence.py --out .release/evidence.json')
    if (status != 0) {
        echo "AI notes: evidence.py exited ${status}; the release carries on without it"
    }
}

void afterChange(Map cfg) {
    try {
        sh(returnStatus: true, script: 'python3 scripts/evidence.py --out .release/evidence.json --change')
        archiveArtifacts artifacts: '.release/evidence.json', allowEmptyArchive: true
        if (state('change.type') == 'normal' && cfg.ai_notes?.change_summary) {
            start('summary', [:])
        }
    } catch (error) {
        echo "AI notes: no summary job (${error.message}); the release carries on"
    }
}

void cause(String previous, long rolledBackAt) {
    if (env.AI_NOTES != 'on') {
        return
    }
    try {
        if (serviceConfig(params.SERVICE).ai_notes?.likely_cause) {
            start('cause', [FAILED_LAMBDA_VERSION: state('deploys.production.number'), ROLLED_BACK_TO: previous,
                            DEPLOYED_AT: state('deploys.production.deployed_at'), ROLLED_BACK_AT: rolledBackAt])
        }
    } catch (error) {
        echo "AI notes: no likely cause job (${error.message})"
    }
}

// The ai-notes job, started and left: a missing job only prints.
private void start(String kind, Map extra) {
    try {
        List parameters = [
            string(name: 'KIND', value: kind),
            string(name: 'SERVICE', value: params.SERVICE),
            string(name: 'RELEASE_BUILD', value: env.BUILD_NUMBER),
            string(name: 'CHANGE_SYS_ID', value: state('change.sys_id')),
            string(name: 'CHANGE_NUMBER', value: state('change.number')),
            string(name: 'JIRA_KEY', value: params.JIRA_KEY ?: ''),
            string(name: 'SLACK_CHANNEL', value: state('slack.channel')),
            string(name: 'SLACK_TS', value: state('slack.ts')),
        ]
        for (String name : extra.keySet() as List) {
            parameters << string(name: name, value: String.valueOf(extra[name]))
        }
        build job: 'ai-notes', wait: false, propagate: false, parameters: parameters
        echo "AI notes: ai-notes started for the ${kind}"
    } catch (error) {
        echo "AI notes: the ai-notes job didn't start (${error.message}); the release carries on"
    }
}

private String state(String key) {
    return sh(returnStdout: true, script: "python3 scripts/run_state.py get ${key}").trim()
}
