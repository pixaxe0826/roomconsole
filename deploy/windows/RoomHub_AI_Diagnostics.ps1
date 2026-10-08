param([string]$Root = 'C:\RoomHubAI')
$ErrorActionPreference = 'Stop'
# Run elevated when the existing scheduled task uses Highest. Do not print SSH
# command lines, which may contain credential paths. Never stop by process name.
$items = @()
foreach ($name in @('llama-server.exe','whisper-server.exe')) {
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='$name'")) {
        if (-not $p.CommandLine) {
            $items += [pscustomobject]@{ process=$name; pid=$p.ProcessId; state='arguments_unreadable_run_elevated'; model_argument=$null; port=$null }
            continue
        }
        if (-not $p.ExecutablePath -or -not $p.ExecutablePath.StartsWith(($Root.TrimEnd('\')+'\'), [StringComparison]::OrdinalIgnoreCase)) { continue }
        $model = $null; $port = $null
        if ($p.CommandLine -match '(?:^|\s)(?:-m|--model)\s+(?:"([^"]+)"|([^\s]+))') {
            $value = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
            $model = [IO.Path]::GetFileName($value)
        }
        if ($p.CommandLine -match '(?:^|\s)--port\s+(\d+)') { $port = [int]$Matches[1] }
        $items += [pscustomobject]@{
            process=$name; pid=$p.ProcessId; state='process_present'; model_argument=$model; port=$port
            identity_source='process_command_line_not_weight_verification'; device_verified=$false; started_at=$p.CreationDate
        }
    }
}
[pscustomobject]@{
    scope='Read-only local process arguments; not inference/CUDA/SSH delivery proof'
    processes=$items
    scheduler=(Get-ScheduledTask -TaskName 'RoomHub AI Bridge' -ErrorAction SilentlyContinue | Select-Object TaskName,State)
} | ConvertTo-Json -Depth 6
