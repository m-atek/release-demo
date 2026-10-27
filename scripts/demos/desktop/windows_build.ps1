# The Windows track of store-assistant, on the Windows build agent: test, or
# build, sign and verify the MSI.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/demos/desktop/windows_build.ps1 -Step test -Service store-assistant
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/demos/desktop/windows_build.ps1 -Step build -Service store-assistant
#
# test runs the xUnit tests with dotnet test and leaves a .trx report and the
# Cobertura coverage in dist/<service>/parts/windows/reports/.
#
# build publishes the WPF app (.NET 10, win-x64, framework-dependent), signs
# StoreAssistant.exe, packages it as an MSI with the WiX Toolset SDK (version
# from services.yaml, windows.wix_sdk), signs the MSI, verifies the signature
# with signtool verify /pa and Get-AuthenticodeSignature, and writes
# StoreAssistant-<version>.msi and windows-build.json to
# dist/<service>/parts/windows/. Signing goes through
# scripts/demos/standins/artifact_signing.ps1: Azure Artifact Signing, or with
# STANDIN=1 (STANDIN_MICROSOFT overrides) the agent's test certificate.
#
# Windows PowerShell 5.1, which every Windows Server has.

param(
    [Parameter(Mandatory)] [ValidateSet('test', 'build')] [string] $Step,
    [Parameter(Mandatory)] [string] $Service
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
. (Join-Path $Root 'scripts\demos\standins\artifact_signing.ps1')

$Source = Join-Path $Root "services\$Service\windows"
$Out = Join-Path $Root "dist\$Service\parts\windows"
$Version = (Get-Content (Join-Path $Root "services\$Service\VERSION") -Raw).Trim()
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$env:CI = 'true'

function Invoke-Native([string] $What, [scriptblock] $Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

# A value from the service's settings in services.yaml (the environment wins), read without a YAML module.
function Get-Setting([string] $Name, [string] $Default) {
    $fromEnvironment = [Environment]::GetEnvironmentVariable($Name)
    if ($null -ne $fromEnvironment) { return $fromEnvironment }
    $text = Get-Content (Join-Path $Root 'services.yaml') -Raw
    $block = [regex]::Match($text, "(?m)^$([regex]::Escape($Service)):\r?\n((?:[ #].*\r?\n|\r?\n)*)")
    if ($block.Success) {
        $match = [regex]::Match($block.Groups[1].Value, "(?m)^\s+$([regex]::Escape($Name)):\s*`"?([^`"\r\n#]*)`"?")
        if ($match.Success) { return $match.Groups[1].Value.Trim() }
    }
    return $Default
}

function Get-Sha([string] $Path, [string] $Algorithm) {
    return (Get-FileHash -Path $Path -Algorithm $Algorithm).Hash.ToLower()
}

if ($Step -eq 'test') {
    $reports = Join-Path $Out 'reports'
    if (Test-Path $reports) { Remove-Item $reports -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $reports | Out-Null
    Write-Host "Testing Store Assistant $Version for Windows with dotnet test ($(dotnet --version))"
    & dotnet test (Join-Path $Source 'StoreAssistant.Core.Tests\StoreAssistant.Core.Tests.csproj') -c Release `
        --logger 'trx;LogFileName=windows.trx' --results-directory $reports --collect 'XPlat Code Coverage'
    $code = $LASTEXITCODE
    Get-ChildItem $reports -Recurse -Filter coverage.cobertura.xml | Select-Object -First 1 |
        ForEach-Object { Copy-Item $_.FullName (Join-Path $reports 'coverage-windows.cobertura.xml') }
    if ($code -ne 0) { throw "The Windows tests failed (dotnet test exit code $code)." }
    exit 0
}

$started = Get-Date
$standIn = (Get-Setting 'STANDIN_MICROSOFT' (Get-Setting 'STANDIN' '1')) -notin '0', 'false', 'no', ''
$wixSdk = Get-Setting 'wix_sdk' 'WixToolset.Sdk/6.0.2'
$build = if ($env:BUILD_NUMBER) { $env:BUILD_NUMBER } else { '1' }
Write-Host "Store Assistant $Version for Windows, build $build"
if ($standIn) { Write-Host 'STANDIN=1: the Artifact Signing stand-in signs with this agent''s test certificate' }

$work = Join-Path ([IO.Path]::GetTempPath()) "store-assistant-$build-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Get-ChildItem $Out -Filter *.msi | Remove-Item -Force
try {
    $publish = Join-Path $work 'publish'
    Write-Host "dotnet publish, .NET $(dotnet --version)"
    Invoke-Native 'dotnet publish' { dotnet publish (Join-Path $Source 'StoreAssistant\StoreAssistant.csproj') -c Release `
        -r win-x64 --self-contained false "-p:Version=$Version" -o $publish --nologo }
    Invoke-CodeSigning -Path (Join-Path $publish 'StoreAssistant.exe') -StandIn $standIn -Description 'Store Assistant'

    # The installer project, with the WiX SDK version from the settings in its own global.json.
    $installer = Join-Path $work 'installer'
    Copy-Item (Join-Path $Source 'installer') $installer -Recurse
    Copy-Item (Join-Path $Source 'Directory.Build.props') $work
    $sdkName, $sdkVersion = $wixSdk -split '/', 2
    (Get-Content (Join-Path $installer 'StoreAssistant.Installer.wixproj') -Raw) -replace 'Sdk="WixToolset\.Sdk/[^"]*"', "Sdk=`"$sdkName`"" |
        Set-Content (Join-Path $installer 'StoreAssistant.Installer.wixproj') -Encoding UTF8
    @{ 'msbuild-sdks' = @{ $sdkName = $sdkVersion } } | ConvertTo-Json | Set-Content (Join-Path $installer 'global.json') -Encoding UTF8
    Write-Host "WiX: $wixSdk"
    Invoke-Native 'the MSI build' { dotnet build (Join-Path $installer 'StoreAssistant.Installer.wixproj') -c Release `
        "-p:Version=$Version" "-p:PublishDir=$publish" -o (Join-Path $work 'msi') --nologo }
    $msi = Join-Path $Out "StoreAssistant-$Version.msi"
    Copy-Item (Join-Path $work "msi\StoreAssistant-$Version.msi") $msi
    Invoke-CodeSigning -Path $msi -StandIn $standIn -Description 'Store Assistant'
    $signature = Test-CodeSignature -Path $msi
    if ($signature.status -ne 'Valid') { throw "The MSI's signature is $($signature.status)." }
    $label = if ($signature.test_certificate) { ', test certificate' } else { '' }
    Write-Host "Signed by $($signature.signer)$label. Signature: $($signature.status), timestamped by $($signature.timestamp_by)"
} finally {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}

$record = [ordered]@{
    service   = $Service
    version   = $Version
    build_number = $build
    toolchain = ".NET SDK $(dotnet --version), $wixSdk"
    standin   = $standIn
    signature = $signature
    msi       = [ordered]@{ name = (Split-Path $msi -Leaf); size_bytes = (Get-Item $msi).Length
                            sha256 = (Get-Sha $msi 'SHA256'); sha512 = (Get-Sha $msi 'SHA512') }
    built_at  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    seconds   = [int]((Get-Date) - $started).TotalSeconds
}
$record | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $Out 'windows-build.json') -Encoding UTF8
Write-Host "Built dist\$Service\parts\windows\$(Split-Path $msi -Leaf) in $($record.seconds)s"
Write-Host "sha256 $($record.msi.sha256)"
