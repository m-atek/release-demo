// Helpers for demo 2's two Jenkinsfiles, loaded with load: the Terraform
// toolchain image, the GitHub check runs, and one timed stage that keeps the
// Slack message, the events and the change's work notes up to date.

// The checks the ruleset on main requires, by the names GitHub shows.
List<String> checks() {
    return ['terraform fmt', 'terraform validate', 'tflint', 'Checkov', 'Conftest']
}

// Built on the agent from jenkins/toolchain/terraform.Dockerfile and tagged
// with a hash of its inputs, so a change to either builds a new one.
def image() {
    String hash = sh(returnStdout: true,
        script: 'cat jenkins/toolchain/terraform.Dockerfile infra/demos/2-terraform/checks/tflint.hcl | sha256sum | cut -c1-12').trim()
    return docker.build("release-demo/terraform-tools:${hash}",
        '-f jenkins/toolchain/terraform.Dockerfile infra/demos/2-terraform/checks')
}

// Shows the checks as running on the pull request, as soon as the build starts.
void started(List<String> names) {
    for (String name : names) {
        publishChecks(name: name, title: 'Running', summary: 'Jenkins is running this check.',
            status: 'IN_PROGRESS', detailsURL: env.BUILD_URL)
    }
}

// Publishes each check from the file checks.py or pr.py wrote for it.
void publish(List<String> names) {
    for (String name : names) {
        String path = ".release/checks/${name.toLowerCase().replace(' ', '-')}.json"
        if (!fileExists(path)) {
            continue
        }
        def result = readJSON(file: path)
        publishChecks(name: name, title: result.title, summary: result.summary, text: result.text ?: '',
            status: 'COMPLETED', conclusion: result.conclusion == 'success' ? 'SUCCESS' : 'FAILURE',
            detailsURL: env.BUILD_URL)
    }
}

// One stage of the pull request pipeline. Before it runs, Slack shows it
// running; after, pr.py records ok or failed, with its time.
void step(String name, Closure body) {
    stage(name) {
        sh "python3 scripts/demos/terraform/pr.py stage '${name}' running"
        try {
            body()
        } catch (error) {
            sh(returnStatus: true, script: "python3 scripts/demos/terraform/pr.py stage '${name}' failed")
            throw error
        }
        sh "python3 scripts/demos/terraform/pr.py stage '${name}' ok"
    }
}

return this
