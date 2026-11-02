// Video 27's error budget: the stage "Error budget", first of all, before Build.
// It counts production's requests and failures from CloudWatch and refuses a
// release when the budget is spent, unless this is the budget's own issue with a
// small change, or someone asked for override: a Normal change only Alex can
// approve, with a reason. With the switch off (scripts/demos/error_budget/switch.py),
// or for a service whose scripts name no error_budget, there is no stage, no line
// and no event. Jenkins only: the other CIs stay down while this demo is up.
//
//   boolean budgetOn = errorBudget.on(cfg)     once, after the run state exists: adds the stage
//   errorBudget.run(cfg)                       in the stage
//   errorBudget.approved(cfg)                  after the approved line
//   errorBudget.released(cfg)                  after Record

boolean on(Map cfg) {
    if (!cfg.scripts?.error_budget) {
        return false
    }
    try {
        boolean enabled = sh(returnStdout: true, script: 'python3 scripts/demos/error_budget/switch.py is-on "$SERVICE"').trim() == 'on'
        if (enabled) {
            sh "${serviceConfig.command(cfg, 'error_budget')} \"\$SERVICE\" --add-stage"
        }
        return enabled
    } catch (error) {
        echo "Error budget: off, the switch couldn't be read (${error.message})"
        return false
    }
}

// Exit 0 passes, exit 3 refuses. Should the script itself break, the change
// becomes a Normal one and a person decides: toward a person, never silent.
void run(Map cfg) {
    String check = serviceConfig.command(cfg, 'error_budget')
    int status = sh(returnStatus: true, script: "${check} \"\$SERVICE\"")
    if (status != 0 && status != 3) {
        sh(returnStatus: true, script: 'python3 scripts/run_state.py set force_normal=true')
        sh(returnStatus: true, script: "${check} \"\$SERVICE\" --no-reading 'the check itself failed, see the build log'")
    }
    archiveArtifacts artifacts: '.release/reports/error-budget.json', allowEmptyArchive: true
    if (status == 3) {
        error 'Error budget spent'
    }
}

// An override with no reason stops here, and stopped() cancels the change.
void approved(Map cfg) {
    int status = sh(returnStatus: true, script: "${serviceConfig.command(cfg, 'error_budget')} approved")
    if (status != 0) {
        error 'Override refused: no reason in the approval'
    }
}

// A mark on the chart: nothing after it depends on this.
void released(Map cfg) {
    sh(returnStatus: true, script: "${serviceConfig.command(cfg, 'error_budget')} released")
}
