# Writes the store's own settings from the fleet table: the drift planted
# (no UploadEndpoint on the drifted server) by reset.py, or fixed by fix-drift.
# The running service reads them at its next /health; no restart.
$me = Get-ThisServer
Write-StoreConfig $me.name $me.endpoint
$keys = @(Get-StoreConfigKeys)
Say "$($me.name): store.config has $($keys -join ', ')"
Write-Result @{ ok = $true; server = $me.name; config_keys = $keys }
