# What a server runs now: for the plan, the fleet check and status.
$health = Get-Health
$version = Get-CurrentVersion
Say "${ServiceName}: $(Get-ServiceState), on $version; /health answers $($health.code) with $($health.version)"
Write-Result @{ version = $version; service = (Get-ServiceState); installed = @(Get-InstalledVersions);
                config_keys = @(Get-StoreConfigKeys); health_code = $health.code; health_version = $health.version;
                health_status = $health.status; problems = @($health.problems); pending_reboot = @(Get-PendingReboot) }
