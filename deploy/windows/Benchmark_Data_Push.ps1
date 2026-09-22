#requires -Version 5.1
<#
Copy a locally validated data-only suite to V35 over authenticated OpenSSH.
Run on the user's Windows PC, not in ChatGPT. Python 3.11+ with Room Hub's
existing timezone data is required locally. No package/model installation.
#>
[CmdletBinding()]
param(
    [string]$SourcePath = 'F:\room console\Benchmark\room_hub_benchmark_v1_250',
    [string]$Suite = 'room_hub_v1',
    [string]$SshHost,
    [string]$SshUser,
    [ValidateRange(1,65535)][int]$SshPort = 8022,
    [string]$IdentityFile,
    [string]$PythonExe = 'python',
    [switch]$Force,
    [switch]$PrepareOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:BenchmarkRepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))

function Invoke-BenchmarkNative {
    param([string]$Program, [string[]]$Arguments, [string]$InputText)
    # Array splatting preserves space-containing arguments. No Invoke-Expression.
    if ($null -ne $InputText -and $InputText.Length -gt 0) {
        $InputText | & $Program @Arguments
    } else {
        & $Program @Arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "Native command failed: $Program (exit $LASTEXITCODE)" }
}

function Get-BenchmarkTransport {
    param([string]$Server, [string]$User, [int]$Port, [string]$KeyFile)
    # Hostname/IPv4 or an SSH config alias only; options/IPv6/whitespace refused.
    if ($Server -notmatch '\A[A-Za-z0-9][A-Za-z0-9.-]{0,252}\z' -or
        $User -notmatch '\A[A-Za-z_][A-Za-z0-9_-]{0,63}\z' -or
        $Port -lt 1 -or $Port -gt 65535) { throw 'Invalid SSH host, user or port.' }
    $sshArgs = @('-p', [string]$Port, '-o', 'StrictHostKeyChecking=yes')
    $scpArgs = @('-P', [string]$Port, '-o', 'StrictHostKeyChecking=yes')
    if ($KeyFile) {
        if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) { throw 'Identity file does not exist.' }
        $keyPath = (Resolve-Path -LiteralPath $KeyFile).ProviderPath
        $sshArgs += @('-i', $keyPath); $scpArgs += @('-i', $keyPath)
    }
    return @{ Target = "${User}@${Server}"; Ssh = $sshArgs; Scp = $scpArgs }
}

function Invoke-BenchmarkDataPush {
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Container)) { throw 'SourcePath does not exist on this Windows PC.' }
    if ($Suite -notmatch '\A[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\z') { throw 'Invalid suite name.' }
    $null = Get-Command $PythonExe -ErrorAction Stop
    $transport = $null
    if (-not $PrepareOnly) {
        if (-not $SshHost) { $SshHost = Read-Host 'V35 SSH hostname or IP' }
        if (-not $SshUser) { $SshUser = Read-Host 'Termux SSH username' }
        $transport = Get-BenchmarkTransport $SshHost $SshUser $SshPort $IdentityFile
        $null = Get-Command ssh -CommandType Application -ErrorAction Stop
        $null = Get-Command scp -CommandType Application -ErrorAction Stop
    }
    $token = [Guid]::NewGuid().ToString('N')
    $temp = Join-Path ([IO.Path]::GetTempPath()) "room-hub-benchmark-$token"
    $keep = $false
    $previousUtf8 = $env:PYTHONUTF8
    $previousOutput = [Console]::OutputEncoding
    $previousPipeEncoding = $OutputEncoding
    Push-Location -LiteralPath $script:BenchmarkRepoRoot
    try {
        $env:PYTHONUTF8 = '1'
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $OutputEncoding = [Text.UTF8Encoding]::new($false)
        $preparedText = @(Invoke-BenchmarkNative -Program $PythonExe -Arguments @(
            '-m', 'benchmarks.transfer', 'prepare', '--source', $SourcePath,
            '--output', $temp, '--suite', $Suite))
        $prepared = ($preparedText -join "`n") | ConvertFrom-Json
        if ($prepared.archive_sha256 -notmatch '\A[0-9a-f]{64}\z') { throw 'Invalid local package response.' }
        if ($PrepareOnly) {
            $keep = $true
            $prepared | ConvertTo-Json -Depth 5
            return
        }
        # Only fixed source text and validated hexadecimal arguments enter the remote shell.
        $initScript = 'set -eu' + "`n" + 'exec bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" prepare ' + $token + "`n"
        $null = Invoke-BenchmarkNative -Program 'ssh' -Arguments ($transport.Ssh + @($transport.Target, 'bash -s')) -InputText $initScript
        $destination = $transport.Target + ':room-hub-benchmark-data/.incoming/' + $token + '/bundle.zip'
        Invoke-BenchmarkNative -Program 'scp' -Arguments ($transport.Scp + @($prepared.archive, $destination))
        $installScript = 'set -eu' + "`n" + 'exec bash "$HOME/room-hub/deploy/termux/benchmark-data.sh" install ' + $token + ' ' + $prepared.archive_sha256
        if ($Force) { $installScript += ' --force' }
        $installScript += "`n"
        $installedText = @(Invoke-BenchmarkNative -Program 'ssh' -Arguments ($transport.Ssh + @($transport.Target, 'bash -s')) -InputText $installScript)
        $installed = ($installedText -join "`n") | ConvertFrom-Json
        if ($installed.dataset_hash -ne $prepared.dataset_hash -or $installed.suite -ne $prepared.suite) {
            throw 'Remote receipt does not match the local dataset digest.'
        }
        $installed | ConvertTo-Json -Depth 5
        Write-Host "Verified external suite: $Suite. No benchmark or application deployment was run."
    } finally {
        [Console]::OutputEncoding = $previousOutput
        $OutputEncoding = $previousPipeEncoding
        if ($null -eq $previousUtf8) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue }
        else { $env:PYTHONUTF8 = $previousUtf8 }
        Pop-Location
        if (-not $keep -and (Test-Path -LiteralPath $temp)) {
            Remove-Item -LiteralPath $temp -Recurse -Force
        }
    }
}

# Dot-source for dependency-free argument/quoting tests; normal execution runs once.
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-BenchmarkDataPush } catch { Write-Error $_; exit 1 }
}
