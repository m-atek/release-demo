// Video 18's cost check: the stage "Cost check" right after Check staging. It
// sends real page views to staging, prices what they used and projects the
// month at the expected volume in services.yaml. It never stops a release
// and never approves one: above the limit, or without a measurement, the
// change asks Cost approvers. With the switch off
// (scripts/demos/cost_gate/switch.py), or for a service whose scripts name
// no cost_check, there is no stage, no line and no attachment.
//
//   boolean costCheckOn = costCheck.on(cfg)     once, after the run state exists: adds the stage
//   costCheck.run(cfg)                          in the stage

boolean on(Map cfg) {
    if (!cfg.scripts?.cost_check) {
        return false
    }
    try {
        boolean enabled = sh(returnStdout: true, script: 'python3 scripts/demos/cost_gate/switch.py is-on "$SERVICE"').trim() == 'on'
        if (enabled) {
            sh "${serviceConfig.command(cfg, 'cost_check')} \"\$SERVICE\" --add-stage"
        }
        return enabled
    } catch (error) {
        echo "Cost check: off, the switch couldn't be read (${error.message})"
        return false
    }
}

// Exit 0 in every outcome; should the script itself break, a cost approver
// decides, as for any run it couldn't measure.
void run(Map cfg) {
    String check = serviceConfig.command(cfg, 'cost_check')
    int status = sh(returnStatus: true, script: "${check} \"\$SERVICE\"")
    if (status != 0) {
        sh(returnStatus: true, script: 'python3 scripts/run_state.py set cost_review=true')
        sh(returnStatus: true, script: "${check} \"\$SERVICE\" --no-measurement 'the check itself failed, see the build log'")
    }
    archiveArtifacts artifacts: '.release/reports/cost.json', allowEmptyArchive: true
}
