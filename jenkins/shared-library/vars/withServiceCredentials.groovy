// Binds the Jenkins credentials a service's deploy and check need, from
// jenkins_credentials in services.yaml (credential id: variable). For OpenID
// Connect credentials (demo 5) each binding mints a fresh ID token for the
// build, so every step gets its own. A service without the key binds nothing
// and runs the body as it is.
//
//   withServiceCredentials(cfg) { sh "${deploy} ${service} staging" }

def call(Map cfg, Closure body) {
    Map credentials = cfg.jenkins_credentials ?: [:]
    if (!credentials) {
        return body()
    }
    List bindings = []
    for (String id : credentials.keySet() as List) {
        bindings << string(credentialsId: id, variable: credentials[id])
    }
    def result = null
    withCredentials(bindings) { result = body() }
    return result
}
