# Pre-checks before an upgrade: free disk, the service's state, a pending
# reboot, and what is installed. fleet.py judges them against services.yaml,
# together with the store's maintenance window.
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$freeGb = [math]::Round($disk.FreeSpace / 1GB, 1)
$pending = @(Get-PendingReboot)
$state = Get-ServiceState
$version = Get-CurrentVersion
Say "Free disk on C: $freeGb GB"
Say "Service ${ServiceName}: $state, on $version"
Say ('Pending reboot: ' + $(if ($pending.Count) { $pending -join ', ' } else { 'none' }))
Write-Result @{ free_gb = $freeGb; service = $state; version = $version; pending_reboot = $pending;
                installed = @(Get-InstalledVersions) }
