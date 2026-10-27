# Windows code signing, real or stand-in, behind one interface. Dot-source it:
#
#   . scripts/demos/standins/artifact_signing.ps1
#   Invoke-CodeSigning -Path StoreAssistant.msi -StandIn $true -Description 'Store Assistant'
#   $result = Test-CodeSignature -Path StoreAssistant.msi     # signer, status, timestamp, test_certificate
#
# The real one is Azure Artifact Signing: signtool with Microsoft's Artifact
# Signing dlib and a metadata.json naming the endpoint, the account and the
# certificate profile (ARTIFACT_SIGNING_ENDPOINT, _ACCOUNT, _PROFILE), signing
# in as the build's Azure identity through demo 5's trust in Jenkins
# (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE), so no Azure
# secret is stored. Microsoft must first validate the organisation, which
# takes days.
#
# The stand-in signs with the same signtool, SHA-256 and an RFC 3161
# timestamp, but with a test certificate the build agent made for itself at
# setup (CN=Expeditious Software (test certificate), private key not
# exportable, in LocalMachine\My) and trusts. The signature says so in its
# subject. Windows PowerShell 5.1.

$script:TestCertificateSubject = 'CN=Expeditious Software (test certificate)'
$script:Timestamp = @{ StandIn = 'http://timestamp.digicert.com'; Azure = 'http://timestamp.acs.microsoft.com' }

function Get-SignTool {
    $found = Get-ChildItem -Path 'C:\tools\signtool' -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } | Select-Object -First 1
    if (-not $found) { throw 'signtool.exe is missing from C:\tools\signtool: the build agent setup installs it.' }
    return $found.FullName
}

function Get-TestCertificate {
    $certificate = Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.Subject -eq $script:TestCertificateSubject -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
        Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $certificate) { throw "No test certificate '$($script:TestCertificateSubject)' on this agent: rerun its setup." }
    return $certificate
}

function Invoke-SignTool([string[]] $Arguments) {
    $output = & (Get-SignTool) @Arguments 2>&1
    $code = $LASTEXITCODE
    $output | ForEach-Object { Write-Host "  $_" }
    if ($code -ne 0) { throw "signtool $($Arguments[0]) failed with exit code $code" }
}

function Invoke-CodeSigning {
    param([Parameter(Mandatory)] [string] $Path, [bool] $StandIn = $true, [string] $Description = 'Store Assistant')
    $name = Split-Path $Path -Leaf
    if ($StandIn) {
        $certificate = Get-TestCertificate
        Write-Host "Signing $name with $($certificate.Subject), SHA-256, timestamped"
        Invoke-SignTool @('sign', '/sha1', $certificate.Thumbprint, '/sm', '/s', 'My', '/fd', 'SHA256',
            '/tr', $script:Timestamp.StandIn, '/td', 'SHA256', '/d', $Description, $Path)
        return
    }
    foreach ($variable in 'ARTIFACT_SIGNING_ENDPOINT', 'ARTIFACT_SIGNING_ACCOUNT', 'ARTIFACT_SIGNING_PROFILE', 'ARTIFACT_SIGNING_DLIB') {
        if (-not [Environment]::GetEnvironmentVariable($variable)) { throw "$variable isn't set: Azure Artifact Signing needs it." }
    }
    $metadata = Join-Path ([IO.Path]::GetTempPath()) "artifact-signing-$PID.json"
    @{ Endpoint = $env:ARTIFACT_SIGNING_ENDPOINT; CodeSigningAccountName = $env:ARTIFACT_SIGNING_ACCOUNT;
       CertificateProfileName = $env:ARTIFACT_SIGNING_PROFILE; ExcludeCredentials = @('ManagedIdentityCredential') } |
        ConvertTo-Json | Set-Content -Path $metadata -Encoding UTF8
    try {
        Write-Host "Signing $name with Azure Artifact Signing ($env:ARTIFACT_SIGNING_ACCOUNT, $env:ARTIFACT_SIGNING_PROFILE)"
        Invoke-SignTool @('sign', '/v', '/fd', 'SHA256', '/tr', $script:Timestamp.Azure, '/td', 'SHA256',
            '/d', $Description, '/dlib', $env:ARTIFACT_SIGNING_DLIB, '/dmdf', $metadata, $Path)
    } finally {
        Remove-Item $metadata -ErrorAction SilentlyContinue
    }
}

function Test-CodeSignature {
    param([Parameter(Mandatory)] [string] $Path)
    Write-Host "Verifying $(Split-Path $Path -Leaf)"
    Invoke-SignTool @('verify', '/pa', '/tw', $Path)
    $signature = Get-AuthenticodeSignature -FilePath $Path
    $certificate = $signature.SignerCertificate
    [pscustomobject]@{
        signer           = $certificate.Subject
        issuer           = $certificate.Issuer
        thumbprint       = $certificate.Thumbprint
        status           = [string] $signature.Status
        timestamped      = [bool] $signature.TimeStamperCertificate
        timestamp_by     = if ($signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Subject } else { $null }
        test_certificate = ($certificate.Subject -eq $script:TestCertificateSubject)
        # A test certificate's public part, so a test machine can trust it. Not a secret.
        certificate      = if ($certificate.Subject -eq $script:TestCertificateSubject) { [Convert]::ToBase64String($certificate.RawData) } else { $null }
    }
}
