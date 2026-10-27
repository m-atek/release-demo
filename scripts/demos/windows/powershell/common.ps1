# Helpers every store-server step shares. fleet.py puts the step's values
# above this file and the step's own script below it, and sends the whole as
# one AWS-RunPowerShellScript command. Windows PowerShell 5.1.
#
# Each step prints what it does for the Run Command output, then one line
# "RESULT <json>" that fleet.py reads. Everything goes through Write-Host, so
# functions that return a value don't return their messages with it. Nothing
# here prints a secret: the JCR token is read from SSM inside the step and only
# ever sent to JCR.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ServiceName = 'StoreSync'

function Say([string] $Line) { Write-Host $Line }

function Write-Result([hashtable] $Values) {
    Write-Host ('RESULT ' + ($Values | ConvertTo-Json -Compress -Depth 6))
}

# The version the service points at, from its executable's folder.
function Get-CurrentVersion {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if (-not $service) { return $null }
    if ($service.PathName -match '\\(\d+\.\d+\.\d+)\\StoreSync\.exe') { return $Matches[1] }
    return 'unknown'
}

function Get-InstalledVersions {
    if (-not (Test-Path $InstallDir)) { return @() }
    return @(Get-ChildItem $InstallDir -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
             Sort-Object { [version]$_.Name } | ForEach-Object { $_.Name })
}

function Get-ServiceState {
    $service = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($service) { return $service.Status.ToString() }
    return 'Missing'
}

# One GET of /health: its status code, version, status and problems.
function Get-Health {
    $answer = @{ code = 0; version = $null; status = $null; problems = @(); body = '' }
    try {
        $request = [System.Net.HttpWebRequest]::Create($HealthUrl)
        $request.Timeout = 5000
        $response = $request.GetResponse()
    } catch {
        # PowerShell wraps the WebException; a 503 still carries its response.
        $exception = $_.Exception
        while ($exception -and -not ($exception -is [System.Net.WebException])) { $exception = $exception.InnerException }
        $response = if ($exception) { $exception.Response } else { $null }
        if (-not $response) { $answer.body = $_.Exception.Message; return $answer }
    }
    try {
        $answer.code = [int]$response.StatusCode
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $answer.body = $reader.ReadToEnd()
    } finally {
        $response.Close()
    }
    try {
        $json = $answer.body | ConvertFrom-Json
        $answer.version = $json.version
        $answer.status = $json.status
        if ($json.problems) { $answer.problems = @($json.problems) }
    } catch {
        # Not JSON: keep the body as it came.
    }
    return $answer
}

# Polls /health until it answers 200 with the expected version, or the time is
# up. A 503 that names a problem with the expected version is a configuration
# error, which doesn't fix itself: after 15 seconds of it, stop waiting.
function Wait-Health([string] $Expected, [int] $TimeoutSeconds) {
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $errorSince = $null
    while ($true) {
        $health = Get-Health
        if ($health.code -eq 200 -and $health.version -eq $Expected) { break }
        if ($health.code -eq 503 -and $health.version -eq $Expected -and $health.problems.Count -gt 0) {
            if ($null -eq $errorSince) { $errorSince = $clock.Elapsed.TotalSeconds }
            if ($clock.Elapsed.TotalSeconds - $errorSince -ge 15) { break }
        } else {
            $errorSince = $null
        }
        if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
        Start-Sleep -Seconds 2
    }
    $ok = ($health.code -eq 200 -and $health.version -eq $Expected)
    $seconds = [math]::Round($clock.Elapsed.TotalSeconds, 1)
    if ($ok) {
        Say "Health: /health reports $Expected after ${seconds}s"
    } else {
        $what = if ($health.code) { "answered $($health.code) with $($health.version)" } else { 'did not answer' }
        Say "Health: /health $what after ${seconds}s, expected $Expected. $($health.problems -join '; ')"
    }
    return @{ ok = $ok; expected = $Expected; code = $health.code; version = $health.version; status = $health.status;
               problems = @($health.problems); seconds = $seconds }
}

function Wait-ServiceStatus([string] $Status, [int] $TimeoutSeconds) {
    (Get-Service $ServiceName).WaitForStatus($Status, [TimeSpan]::FromSeconds($TimeoutSeconds))
}

# Points the service at another version's folder: stop, switch, start.
function Switch-StoreSync([string] $Version) {
    $exe = Join-Path $InstallDir "$Version\StoreSync.exe"
    if (-not (Test-Path $exe)) { throw "$Version isn't installed: no $exe" }
    if ((Get-ServiceState) -ne 'Stopped') {
        Say "Stopping $ServiceName"
        Stop-Service $ServiceName -Force
        Wait-ServiceStatus 'Stopped' 30
    }
    Say "Switching $ServiceName to $Version"
    $null = & sc.exe config $ServiceName binPath= "`"$exe`""
    if ($LASTEXITCODE -ne 0) { throw "sc.exe config failed with exit code $LASTEXITCODE" }
    Say "Starting $ServiceName"
    Start-Service $ServiceName
    Wait-ServiceStatus 'Running' 30
}

# The read-only JCR token, read from SSM with this server's own role. Kept in
# a variable only; never written to disk or output. A signed GetParameter call
# with the role's credentials from IMDSv2: loading AWSPowerShell takes up to a
# minute on a t3.medium, longer than the whole upgrade.
function Get-JcrToken {
    $imds = 'http://169.254.169.254/latest'
    $session = @{ 'X-aws-ec2-metadata-token' = (Invoke-RestMethod -UseBasicParsing -Method Put -Uri "$imds/api/token" `
        -Headers @{ 'X-aws-ec2-metadata-token-ttl-seconds' = '60' }) }
    $role = Invoke-RestMethod -UseBasicParsing -Uri "$imds/meta-data/iam/security-credentials/" -Headers $session
    $credentials = Read-Json (Invoke-RestMethod -UseBasicParsing -Uri "$imds/meta-data/iam/security-credentials/$role" -Headers $session)

    $endpoint = "ssm.$Region.amazonaws.com"
    $target = 'AmazonSSM.GetParameter'
    $type = 'application/x-amz-json-1.1'
    $body = @{ Name = $TokenParameter; WithDecryption = $true } | ConvertTo-Json -Compress
    $now = [DateTime]::UtcNow
    $stamp = $now.ToString('yyyyMMddTHHmmssZ')
    $scope = $now.ToString('yyyyMMdd') + "/$Region/ssm/aws4_request"
    $signedHeaders = 'content-type;host;x-amz-date;x-amz-security-token;x-amz-target'
    $canonical = "POST`n/`n`ncontent-type:$type`nhost:$endpoint`nx-amz-date:$stamp`n" +
                 "x-amz-security-token:$($credentials.Token)`nx-amz-target:$target`n`n$signedHeaders`n$(Get-Sha256Hex $body)"
    $key = [Text.Encoding]::UTF8.GetBytes('AWS4' + $credentials.SecretAccessKey)
    foreach ($part in @($now.ToString('yyyyMMdd'), $Region, 'ssm', 'aws4_request')) { $key = Get-Hmac $key $part }
    $signature = ConvertTo-Hex (Get-Hmac $key "AWS4-HMAC-SHA256`n$stamp`n$scope`n$(Get-Sha256Hex $canonical)")

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $reply = Invoke-RestMethod -UseBasicParsing -Method Post -Uri "https://$endpoint/" -Body $body -ContentType $type -Headers @{
        'X-Amz-Date' = $stamp; 'X-Amz-Target' = $target; 'X-Amz-Security-Token' = $credentials.Token
        Authorization = "AWS4-HMAC-SHA256 Credential=$($credentials.AccessKeyId)/$scope, SignedHeaders=$signedHeaders, Signature=$signature" }
    $value = (Read-Json $reply).Parameter.Value
    if (-not $value -or $value -eq 'unset') { throw "$TokenParameter isn't set: run scripts/demos/windows/jcr_token.py" }
    return $value
}

function Read-Json($Value) { if ($Value -is [string]) { return $Value | ConvertFrom-Json } return $Value }
function ConvertTo-Hex([byte[]] $Bytes) { return -join ($Bytes | ForEach-Object { $_.ToString('x2') }) }
function Get-Sha256Hex([string] $Text) {
    return ConvertTo-Hex ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))
}
function Get-Hmac([byte[]] $Key, [string] $Text) {
    return [Security.Cryptography.HMACSHA256]::new($Key).ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
}

# Downloads a package from JCR into a temporary file and checks its SHA-256.
# Returns the file and its hash; throws on a mismatch.
function Get-Package([string] $Path, [string] $Sha256) {
    $file = Join-Path $env:TEMP ("store-sync-" + [guid]::NewGuid().ToString('N') + '.zip')
    Say "Downloading $Path from JCR"
    $token = Get-JcrToken
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$JcrUrl/artifactory/$Path" -OutFile $file `
            -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 120
    } catch {
        throw "JCR download of $Path failed: $($_.Exception.Message)"
    } finally {
        $token = $null
    }
    $actual = (Get-FileHash $file -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item $file -Force
        throw "SHA-256 mismatch for ${Path}: got $actual, expected $Sha256"
    }
    Say "SHA-256 verified: $actual"
    return @{ file = $file; sha256 = $actual; bytes = (Get-Item $file).Length }
}

# Unpacks a verified package into its own version folder, next to the others.
function Install-Package([hashtable] $Package, [string] $Version) {
    $target = Join-Path $InstallDir $Version
    Say "Installing $Version in $target"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Expand-Archive -Path $Package.file -DestinationPath $target -Force
    Set-Content -Path (Join-Path $target '.sha256') -Value $Package.sha256 -Encoding ASCII
    Remove-Item $Package.file -Force
    $installed = (Get-Content (Join-Path $target 'VERSION') -Raw).Trim()
    if ($installed -ne $Version) { throw "The package holds $installed, not $Version" }
}

# The store's own settings, one folder above the versions, so they stay in
# place across upgrades. Without UploadEndpoint when $Endpoint is empty.
function Write-StoreConfig([string] $StoreId, [string] $Endpoint) {
    $lines = @('<?xml version="1.0" encoding="utf-8"?>',
               '<!-- The store''s own settings. StoreSync.exe.config reads them through appSettings file="..\store.config". -->',
               '<appSettings>',
               "  <add key=`"StoreId`" value=`"$StoreId`" />")
    if ($Endpoint) { $lines += "  <add key=`"UploadEndpoint`" value=`"$Endpoint`" />" }
    $lines += '</appSettings>'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Set-Content -Path (Join-Path $InstallDir 'store.config') -Value $lines -Encoding UTF8
}

function Get-StoreConfigKeys {
    $file = Join-Path $InstallDir 'store.config'
    if (-not (Test-Path $file)) { return @() }
    return @(([xml](Get-Content $file -Raw)).appSettings.add | ForEach-Object { $_.key })
}

# Why Windows wants a restart, if it does.
function Get-PendingReboot {
    $reasons = @()
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
        $reasons += 'component servicing'
    }
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
        $reasons += 'Windows Update'
    }
    return $reasons
}

# This server's row in $Servers, found by its instance id (IMDSv2).
function Get-ThisServer {
    $imds = 'http://169.254.169.254/latest'
    $token = Invoke-RestMethod -UseBasicParsing -Method Put -Uri "$imds/api/token" `
        -Headers @{ 'X-aws-ec2-metadata-token-ttl-seconds' = '60' }
    $instanceId = Invoke-RestMethod -UseBasicParsing -Uri "$imds/meta-data/instance-id" `
        -Headers @{ 'X-aws-ec2-metadata-token' = $token }
    $me = $Servers[$instanceId]
    if (-not $me) { throw "This instance, $instanceId, has no row in the fleet table" }
    return $me
}
