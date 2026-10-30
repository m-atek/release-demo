// The four checks every pull request to main gets (video 16): the release's
// stages 1 to 4, Build, Test, Quality gate and SBOM and scan, each a GitHub
// check run from the pipeline's own app. Run every minute by the job
// pull-requests, one pull request at a time, newest first, never from forks,
// and only while video 16's ruleset on main is on.
//
// What runs comes from main: this file, the shared library, the scripts,
// the toolchain images and services.yaml. Only services/<service>/ comes
// from the pull request's merge commit, so a pull request that edits a
// script gets main's checks anyway, and its reviewer sees the edit.

@Library('release-demo') _

node('tools-host') {
    checkout scm
    String pr = 'python3 scripts/demos/agent_ship/pull_requests.py'
    sh 'rm -rf .release dist'
    String next = sh(returnStdout: true, script: "${pr} next").trim()
    if (!next) {
        currentBuild.result = 'NOT_BUILT'
        currentBuild.description = 'No pull request waits for checks.'
        return
    }
    def todo = readJSON(text: next)
    String service = todo.service ?: ''
    currentBuild.displayName = "#${env.BUILD_NUMBER} ${service ?: 'no service'} PR #${todo.number} ${todo.sha.take(7)}"
    withEnv(["PR=${todo.number}", "PR_SHA=${todo.sha}", "PR_SERVICE=${service}"]) {
        if (!service) {
            sh "${pr} skip \"\$PR\" \"\$PR_SHA\""
            currentBuild.description = 'No service with pull request checks changed: the four checks skipped.'
            return
        }
        sh "${pr} start \"\$PR\" \"\$PR_SHA\""
        Map cfg = serviceConfig(service)
        sh '''
            git fetch --quiet origin "+refs/pull/$PR/merge:refs/remotes/origin/pull-request-merge"
            rm -rf "services/$PR_SERVICE"
            git checkout refs/remotes/origin/pull-request-merge -- "services/$PR_SERVICE"
        '''
        String version = readFile("${cfg.path}/VERSION").trim()
        String failed = ''
        Map steps = [
            'Build': { toolchain.build(service, "PR-${todo.number}", cfg) },
            'Test': { toolchain.test(service, cfg) },
            'Quality gate': {
                String args = sh(returnStdout: true,
                    script: "${pr} sonar-args \"\$PR_SERVICE\" '${version}' \"\$PR\"").trim()
                int gate = 1
                try {
                    toolchain.qualityGate(service, version, cfg, args)
                    gate = 0
                } finally {
                    writeFile file: '.release/reports/quality-gate.txt', text: gate == 0 ? 'Passed' : 'Failed'
                }
            },
            'SBOM and scan': { toolchain.sbomAndScan(service) },
        ]
        for (String name : ['Build', 'Test', 'Quality gate', 'SBOM and scan']) {
            String check = name
            if (failed) {
                sh "${pr} check '${check}' cancelled"
                continue
            }
            stage(check) {
                sh "${pr} check '${check}' in_progress"
                try {
                    steps[check]()
                    sh "${pr} check '${check}' success --service \"\$PR_SERVICE\""
                } catch (error) {
                    sh "${pr} check '${check}' failure --service \"\$PR_SERVICE\""
                    failed = check
                }
            }
        }
        currentBuild.description = failed ? "${failed} failed" : 'All four passed'
        if (failed) {
            currentBuild.result = 'FAILURE'
        }
    }
}
