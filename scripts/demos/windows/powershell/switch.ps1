# Back to a version that is already installed: after a failed check, for
# deploy.py --back, and for /rollback. Switch, start, and check that /health
# reports it within $TimeoutSeconds.
$before = Get-CurrentVersion
try {
    Switch-StoreSync $Target
} catch {
    Say "Failed: $($_.Exception.Message)"
    Write-Result @{ ok = $false; step = 'switch'; error = $_.Exception.Message; before = $before; after = (Get-CurrentVersion) }
    exit 0
}
$health = Wait-Health $Target $TimeoutSeconds
$health.before = $before
$health.after = Get-CurrentVersion
Write-Result $health
