# Brings a store server to the baseline: store-sync $BaselineVersion as the
# Windows service StoreSync, with the store's own config (the drift planted on
# the drifted server). Idempotent: scripts/demo.sh up runs it on a new fleet,
# reset.py on a used one. One command for the whole fleet; each server finds
# its own row in $Servers by its instance id.
$me = Get-ThisServer
Say "$($me.name): store-sync $BaselineVersion"

# Updates arriving during a recording would leave a pending reboot, which the
# pre-checks refuse. The fleet lives for hours and every up builds it from the
# current base image.
$updates = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
New-Item -Path $updates -Force | Out-Null
Set-ItemProperty -Path $updates -Name NoAutoUpdate -Value 1 -Type DWord

Write-StoreConfig $me.name $me.endpoint
if ($me.endpoint) { Say "Config: StoreId $($me.name), UploadEndpoint $($me.endpoint)" }
else { Say "Config: StoreId $($me.name), no UploadEndpoint" }

$marker = Join-Path $InstallDir "$BaselineVersion\.sha256"
if ((Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -eq $BaselineSha256)) {
    Say "$BaselineVersion is already installed, SHA-256 $BaselineSha256"
} else {
    Install-Package (Get-Package $BaselinePath $BaselineSha256) $BaselineVersion
}

if (-not (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'")) {
    Say "Creating the service $ServiceName"
    $exe = Join-Path $InstallDir "$BaselineVersion\StoreSync.exe"
    New-Service -Name $ServiceName -BinaryPathName "`"$exe`"" -DisplayName 'Store Sync' -StartupType Automatic `
        -Description 'store-sync: uploads the store''s sales.' | Out-Null
    $null = & sc.exe failure $ServiceName reset= 86400 actions= restart/5000
}
if ((Get-CurrentVersion) -ne $BaselineVersion -or (Get-ServiceState) -ne 'Running') {
    Switch-StoreSync $BaselineVersion
}
$health = Wait-Health $BaselineVersion 60

# A reboot left pending by the base image: restart once, and SSM runs this
# command again after the restart (exit code 3010).
$pending = @(Get-PendingReboot)
$rebooted = Join-Path $InstallDir 'bootstrap-rebooted'
if ($pending.Count -and -not (Test-Path $rebooted)) {
    Say "Pending reboot ($($pending -join ', ')): restarting once, then this runs again"
    Set-Content -Path $rebooted -Value (Get-Date -Format o)
    exit 3010
}

$autoStop = [bool](Get-ScheduledTask -TaskName 'StoreFleetAutoStop' -ErrorAction SilentlyContinue)
Say ('Auto-stop 4 hours after boot: ' + $(if ($autoStop) { 'set' } else { 'missing' }))
Write-Result @{ ok = $health.ok; server = $me.name; version = (Get-CurrentVersion); health = $health;
                drift = (-not $me.endpoint); auto_stop = $autoStop; pending_reboot = $pending }
