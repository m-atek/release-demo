// Video 30's package age: the stage "Package age", first of all, before Build, for
// the versions services.yaml's package_age.versions names (run_state.stages_for).
// It asks pip what the build would install and PyPI when each file was uploaded, and
// refuses a release while one is younger than the rule. Jenkins only: the other CIs
// release versions without the stage.
//
//   packageAge.run(cfg)         in the stage
//   packageAge.approved(cfg)    after the approved line

// Exit 0 passes, exit 3 refuses. Should the script itself break, the change
// becomes a Normal one and a person decides: toward a person, never silent.
void run(Map cfg) {
    String check = serviceConfig.command(cfg, 'package_age')
    int status = sh(returnStatus: true, script: "${check} \"\$SERVICE\"")
    if (status != 0 && status != 3) {
        sh(returnStatus: true, script: 'python3 scripts/run_state.py set force_normal=true')
        sh(returnStatus: true, script: "${check} \"\$SERVICE\" --no-reading 'the check itself failed, see the build log'")
    }
    archiveArtifacts artifacts: '.release/reports/package-age.json', allowEmptyArchive: true
    if (status == 3) {
        error 'Package younger than the rule'
    }
}

// An override with no reason stops here, and stopped() cancels the change.
void approved(Map cfg) {
    int status = sh(returnStatus: true, script: "${serviceConfig.command(cfg, 'package_age')} approved")
    if (status != 0) {
        error 'Override refused: no reason in the approval'
    }
}
