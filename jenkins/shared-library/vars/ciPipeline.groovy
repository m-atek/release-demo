// On every push to main, found by polling every 5 minutes: build, test,
// quality gate, SBOM and scan for every service. Commits marked [skip ci],
// such as the ones stage.py makes, are skipped.

def call() {
    node('tools-host') {
        def checkedOut = checkout scm
        env.GIT_COMMIT = checkedOut.GIT_COMMIT
        String message = sh(returnStdout: true, script: 'git log -1 --format=%B').trim()
        if (message.contains('[skip ci]')) {
            currentBuild.result = 'NOT_BUILT'
            currentBuild.description = 'Skipped: [skip ci]'
            return
        }
        def services = readYaml(file: 'services.yaml')
        for (String service : services.keySet() as List) {
            String version = readFile("${services[service].path}/VERSION").trim()
            sh 'rm -rf .release dist'
            stage("${service}: Build") { toolchain.build(service, 'CI') }
            stage("${service}: Test") { toolchain.test(service) }
            stage("${service}: Quality gate") { toolchain.qualityGate(service, version) }
            stage("${service}: SBOM and scan") { toolchain.sbomAndScan(service) }
        }
    }
}
