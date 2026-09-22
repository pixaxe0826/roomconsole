#requires -Version 5.1
param([Parameter(Mandatory=$true)][string]$PythonExe,
      [Parameter(Mandatory=$true)][string]$SourceRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$helper = Join-Path $PSScriptRoot '../deploy/windows/Benchmark_Data_Push.ps1'
$tokens = $null; $errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($helper, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'PowerShell parser errors.' }
. $helper -PythonExe $PythonExe -SourcePath $SourceRoot -Suite synthetic -SshHost v35-test.local -SshUser u0_a123
$t = Get-BenchmarkTransport '192.0.2.10' 'u0_a123' 8022 ''
if ($t.Target -ne 'u0_a123@192.0.2.10' -or $t.Scp[0] -ne '-P' -or $t.Ssh[0] -ne '-p') { throw 'Port/target mismatch' }
foreach ($bad in @('-oProxyCommand=x', 'host;echo', '$(bad)', 'host user', "host`nnext")) {
    $rejected = $false
    try { $null = Get-BenchmarkTransport $bad 'u0_a123' 8022 '' } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unsafe hostname accepted' }
}
$temp = Join-Path ([IO.Path]::GetTempPath()) ('argv-check-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $temp
try {
    $echo = Join-Path $temp 'argument echo.py'
    [IO.File]::WriteAllText($echo, 'import sys,json; print(json.dumps(sys.argv[1:]))')
    $spaced = 'F:\room console\Benchmark\room_hub_benchmark_v1_250'
    $unicode = 'folder with space\' + [char]0xD55C + [char]0xAE00
    $actual = @(Invoke-BenchmarkNative $PythonExe @($echo, $spaced, $unicode, 'plain')) -join "`n"
    $got = @($actual | ConvertFrom-Json)
    if ($got.Count -ne 3 -or $got[0] -ne $spaced -or $got[1] -ne $unicode) { throw 'Native argument quoting failed' }
} finally { Remove-Item -LiteralPath $temp -Force -Recurse }

# Intercept the native boundary: local Python really validates generated files;
# ssh/scp are stubs and never contact a remote machine.
$script:realNative = ${function:Invoke-BenchmarkNative}
$script:calls = [Collections.Generic.List[object]]::new()
$script:prepared = $null
$script:failScp = $false
function Invoke-BenchmarkNative {
    param([string]$Program, [string[]]$Arguments, [string]$InputText)
    $script:calls.Add(@{ Program=$Program; Arguments=$Arguments; InputText=$InputText })
    if ($Program -eq 'scp') {
        if ($script:failScp) { throw 'simulated scp failure' }
        if (-not (Test-Path -LiteralPath $Arguments[-2])) { throw 'Package missing during SCP' }
        return
    }
    if ($Program -eq 'ssh') {
        if ($InputText -match ' install ') {
            return (@{suite=$script:prepared.suite; dataset_hash=$script:prepared.dataset_hash} | ConvertTo-Json -Compress)
        }
        return '{"staging":"synthetic-only"}'
    }
    $output = @(& $script:realNative -Program $Program -Arguments $Arguments -InputText $InputText)
    $script:prepared = ($output -join "`n") | ConvertFrom-Json
    return $output
}
$null = Invoke-BenchmarkDataPush
if (@($script:calls | Where-Object {$_.Program -eq 'scp'}).Count -ne 1) { throw 'SCP count wrong' }
$install = @($script:calls | Where-Object {$_.Program -eq 'ssh' -and $_.InputText -match ' install '})
if ($install.Count -ne 1 -or $install[0].InputText -match '--force') { throw 'Unexpected overwrite' }
if (Test-Path -LiteralPath $script:prepared.archive) { throw 'Temporary package leaked' }
$script:calls.Clear(); $Force=$true
$null=Invoke-BenchmarkDataPush
$install=@($script:calls | Where-Object {$_.Program -eq 'ssh' -and $_.InputText -match ' install '})
if ($install[0].InputText -notmatch '--force') { throw 'Explicit force not forwarded' }
$script:calls.Clear(); $script:failScp=$true; $failed=$false
try { $null=Invoke-BenchmarkDataPush } catch { $failed=$true }
if (-not $failed) { throw 'SCP failure was swallowed' }
if (@($script:calls | Where-Object {$_.Program -eq 'ssh' -and $_.InputText -match ' install '}).Count) { throw 'Partial upload promoted' }
if (Test-Path -LiteralPath $script:prepared.archive) { throw 'Failed transfer local package leaked' }
Write-Output 'POWERSHELL_TRANSPORT_TESTS_PASSED (generated data, stub SSH/SCP)'
