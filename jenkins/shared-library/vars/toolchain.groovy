// The pinned toolchains the build, test and scan steps run in, shared by the
// release and CI pipelines. Stage logic stays in scripts/, so the same steps
// can run from GitHub Actions, GitLab CI or Azure Pipelines.

import groovy.transform.Field

@Field String SONAR = 'sonarsource/sonar-scanner-cli:12.2@sha256:a3f4215076706c95a17a68c19322ee916e40a3acd081a8c1a1e839e0194afa57'
@Field String SYFT = 'anchore/syft:v1.54.1@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa'
@Field String GRYPE = 'anchore/grype:v0.120.1@sha256:e4a44ef45d285b829ce6efe2642980329661bd2d18eab5fc539138d4adaebbbe'

// The Python image, built from jenkins/toolchain/python.Dockerfile and tagged
// with a hash of its inputs, so a change to either builds a new one.
def python() {
    String hash = sh(returnStdout: true,
        script: 'cat jenkins/toolchain/python.Dockerfile requirements-dev.txt | sha256sum | cut -c1-12').trim()
    return docker.build("release-demo/python-tools:${hash}", '-f jenkins/toolchain/python.Dockerfile .')
}

def build(String service, String jiraKey) {
    python().inside('-e HOME=/tmp') {
        sh "python scripts/build.py ${service} --jira-key ${jiraKey}"
    }
}

def test(String service) {
    python().inside('-e HOME=/tmp') {
        sh """
            mkdir -p .release/reports
            cd services/${service}
            python -m pytest -q -p no:cacheprovider --junitxml=${env.WORKSPACE}/.release/reports/junit.xml \\
                --cov=. --cov-report=xml:${env.WORKSPACE}/.release/reports/coverage.xml
        """
    }
    junit testResults: '.release/reports/junit.xml', allowEmptyResults: false
}

def qualityGate(String service, String version) {
    String args = sh(returnStdout: true, script: "python3 scripts/sonar_args.py ${service} ${version}").trim()
    withCredentials([string(credentialsId: 'sonar-token', variable: 'SONAR_TOKEN')]) {
        docker.image(SONAR).inside('-e SONAR_TOKEN -e SONAR_HOST_URL=https://sonarcloud.io -e SONAR_USER_HOME=/tmp/sonar') {
            sh "sonar-scanner ${args}"
        }
    }
}

// Syft and Grype ship as images without a shell, so they run with docker run.
// Neither image has a /tmp the agent's user can write, so each gets a cache
// directory on the data volume for its cache and temporary files.
def sbomAndScan(String service) {
    String cache = '/data/jenkins-agent/cache'
    sh """
        rm -rf .release/package && mkdir -p .release/package .release/reports ${cache}/syft ${cache}/grype
        python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" dist/${service}/*.zip .release/package
        docker run --rm --user \$(id -u):\$(id -g) -v "\$PWD:/work" -v ${cache}/syft:/cache \\
            -e SYFT_CACHE_DIR=/cache -e TMPDIR=/cache ${SYFT} \\
            dir:/work/.release/package --source-name ${service} -o cyclonedx-json=/work/.release/reports/sbom.cdx.json
        docker run --rm --user \$(id -u):\$(id -g) -v "\$PWD:/work" -v ${cache}/grype:/cache \\
            -e GRYPE_DB_CACHE_DIR=/cache -e TMPDIR=/cache ${GRYPE} sbom:/work/.release/reports/sbom.cdx.json \\
            --fail-on critical -o table -o json=/work/.release/reports/grype.json
    """
}
