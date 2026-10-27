// The pinned toolchains the build, test and scan steps run in, shared by the
// release and CI pipelines. Stage logic stays in scripts/, so the same steps
// can run from GitHub Actions, GitLab CI or Azure Pipelines.
//
// A service builds and tests in the image from its toolchain Dockerfile
// (default jenkins/toolchain/python.Dockerfile) on the tools host, or, with
// build_agent set in services.yaml, directly on that Jenkins agent (a Mac or
// Windows machine), which hands its results back with stash.

import groovy.transform.Field

@Field String SONAR = 'sonarsource/sonar-scanner-cli:12.2@sha256:a3f4215076706c95a17a68c19322ee916e40a3acd081a8c1a1e839e0194afa57'
@Field String SYFT = 'anchore/syft:v1.54.1@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa'
@Field String GRYPE = 'anchore/grype:v0.120.1@sha256:e4a44ef45d285b829ce6efe2642980329661bd2d18eab5fc539138d4adaebbbe'
@Field String PYTHON_DOCKERFILE = 'jenkins/toolchain/python.Dockerfile'

// The toolchain image, built on the agent from its Dockerfile and tagged with
// a hash of its inputs, so a change to either builds a new one.
def image(String dockerfile = PYTHON_DOCKERFILE) {
    String name = dockerfile.tokenize('/').last().replace('.Dockerfile', '')
    String hash = sh(returnStdout: true,
        script: "cat '${dockerfile}' requirements-dev.txt | sha256sum | cut -c1-12").trim()
    return docker.build("release-demo/${name}-tools:${hash}", "-f ${dockerfile} .")
}

// Kept for jobs that ask for the Python toolchain by name.
def python() {
    return image(PYTHON_DOCKERFILE)
}

// Runs body in the service's toolchain: on its build agent, or in its image.
private void inToolchain(Map cfg, String stashName, String includes, Closure body) {
    if (cfg.build_agent) {
        node(cfg.build_agent) {
            checkout scm
            body()
            stash name: stashName, includes: includes, allowEmpty: true
        }
        unstash stashName
    } else {
        image(cfg.toolchain ?: PYTHON_DOCKERFILE).inside('-e HOME=/tmp') { body() }
    }
}

def build(String service, String jiraKey, Map cfg = null) {
    cfg = cfg ?: serviceConfig(service)
    inToolchain(cfg, 'build', "dist/${service}/**") {
        sh "${serviceConfig.command(cfg, 'build')} ${service} --jira-key ${jiraKey}"
    }
}

def test(String service, Map cfg = null) {
    cfg = cfg ?: serviceConfig(service)
    inToolchain(cfg, 'test', '.release/reports/**') {
        if (cfg.scripts.test) {
            sh "mkdir -p .release/reports && ${serviceConfig.command(cfg, 'test')} ${service}"
        } else {
            sh """
                mkdir -p .release/reports
                cd ${cfg.path}
                python -m pytest -q -p no:cacheprovider --junitxml=${env.WORKSPACE}/.release/reports/junit.xml \\
                    --cov=. --cov-report=xml:${env.WORKSPACE}/.release/reports/coverage.xml
            """
        }
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
