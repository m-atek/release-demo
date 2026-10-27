# Installs $Version next to the versions already there and switches the
# service to it. The package is downloaded and its SHA-256 checked before the
# service stops, so a bad package never takes the store offline.
$before = Get-CurrentVersion
try {
    $package = Get-Package $PackagePath $Sha256
} catch {
    Say $_.Exception.Message
    Write-Result @{ ok = $false; step = 'download'; error = $_.Exception.Message; before = $before; after = $before }
    exit 0
}
try {
    Install-Package $package $Version
    Switch-StoreSync $Version
} catch {
    Say "Failed: $($_.Exception.Message)"
    Write-Result @{ ok = $false; step = 'switch'; error = $_.Exception.Message; sha256 = $package.sha256;
                    before = $before; after = (Get-CurrentVersion) }
    exit 0
}
Write-Result @{ ok = $true; sha256 = $package.sha256; bytes = $package.bytes; before = $before; after = (Get-CurrentVersion) }
