// The data-center side of the inventory rollout, on dc-agent: Ansible over SSH
// to inv-dc-01 and inv-dc-02, which only dc-agent reaches. dc-agent connects
// out to Jenkins; nothing connects in. The release on the tools host starts
// this job (scripts/demos/linux/jenkins_job.py) with REQUEST, what to run as
// JSON, at the release's own commit. The servers' results go back as an
// artifact; Splunk, Slack and the change hear about them from the tools host.

node('dc-agent') {
    checkout scm
    def request = readJSON(text: params.REQUEST ?: '{}')
    String commit = request.commit ?: ''
    if (commit ==~ /[0-9a-f]{40}/) {
        withEnv(["COMMIT=${commit}"]) { sh 'git checkout --quiet --detach "$COMMIT"' }
    }
    currentBuild.description = "${request.extra_vars?.action ?: request.playbook} ${request.extra_vars?.version ?: ''} on ${(request.names ?: []).join(', ')}"
    try {
        withCredentials([
            string(credentialsId: 'jcr-inventory-read-token', variable: 'JCR_TOKEN'),
            string(credentialsId: 'cloudflare-dc-agent-client-id', variable: 'CF_ACCESS_CLIENT_ID'),
            string(credentialsId: 'cloudflare-dc-agent-client-secret', variable: 'CF_ACCESS_CLIENT_SECRET'),
            string(credentialsId: 'inventory-app-secret', variable: 'INVENTORY_APP_SECRET'),
        ]) {
            sh '''
                rm -rf .release && mkdir -p .release
                INVENTORY_DC_HERE=1 python3 scripts/demos/linux/datacenter.py --request-env REQUEST --results .release/dc-results.jsonl
            '''
        }
    } finally {
        archiveArtifacts artifacts: '.release/dc-results.jsonl', allowEmptyArchive: true
    }
}
