// A service's entry in services.yaml, as docs/adding-a-demo.md describes it,
// with the Lambda service's stage scripts as defaults.
//
//   Map cfg = serviceConfig('orders-service')
//   sh "${serviceConfig.command(cfg, 'deploy')} orders-service staging"

Map call(String service) {
    Map all = readYaml(file: 'services.yaml')
    if (!all.containsKey(service)) {
        error "Unknown service ${service}. services.yaml has: ${all.keySet().join(', ')}"
    }
    Map cfg = all[service]
    Map defaults = [build: 'scripts/build.py', publish: 'scripts/publish.py',
                    deploy: 'scripts/deploy.py', check: 'scripts/check.py']
    cfg.scripts = defaults + (cfg.scripts ?: [:])
    return cfg
}

// The shell command that runs one of the service's stage scripts from the
// repository root: Python scripts through python3, anything else directly.
String command(Map cfg, String stage) {
    String path = cfg.scripts[stage]
    return path.endsWith('.py') ? "python3 ${path}" : "./${path}"
}
