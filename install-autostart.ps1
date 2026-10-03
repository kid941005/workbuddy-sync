# =====================================================================
#  WorkBuddy auto-sync : installer
#
#  Does two things:
#    1) Points WorkBuddy's "start at logon" entry at wb-launch.cmd,
#       so the cloud PULL happens BEFORE WorkBuddy opens
#       (this is the only safe window: WorkBuddy must not hold the DB).
#    2) Registers a daily scheduled task that runs a PUSH
#       (push is read-only, safe to run while WorkBuddy is open).
#
#  Everything is backed up and can be undone with uninstall-autostart.cmd
# =====================================================================
[CmdletBinding()]
param(
    [string]$PushTimes = '12:30,18:30'
)

$ErrorActionPreference = 'Stop'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName  = 'WorkBuddy.WorkBuddy'
$TaskName = 'WorkBuddy-Sync-Push'

function Say($m) { Write-Host $m }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg  = Join-Path $env:LOCALAPPDATA 'wb-sync'
if (-not (Test-Path $cfg)) { New-Item -ItemType Directory -Path $cfg -Force | Out-Null }

$launcher = Join-Path $root 'wb-launch.cmd'
$pushCmd  = Join-Path $root 'push-silent.cmd'

Say ''
Say '=================================================='
Say ' WorkBuddy auto-sync - install'
Say '=================================================='
Say " Sync folder : $root"
Say " Config dir  : $cfg"
Say ''

# ---------- 0) sanity ----------
foreach ($f in @($launcher, $pushCmd, (Join-Path $root 'wb_sync.py'))) {
    if (-not (Test-Path $f)) {
        Say "[ERROR] missing file: $f"
        Say '        Make sure OneDrive has fully synced this folder.'
        exit 1
    }
}

# ---------- 1) locate WorkBuddy.exe ----------
$cands = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\WorkBuddy\WorkBuddy.exe'),
    (Join-Path $env:ProgramFiles 'WorkBuddy\WorkBuddy.exe')
)
if (${env:ProgramFiles(x86)}) {
    $cands += (Join-Path ${env:ProgramFiles(x86)} 'WorkBuddy\WorkBuddy.exe')
}
$wb = $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $wb) {
    $cur = (Get-ItemProperty -Path $RunKey -ErrorAction SilentlyContinue).$RunName
    if ($cur) {
        $guess = $cur.Trim('"')
        if (Test-Path $guess) { $wb = $guess }
    }
}
if (-not $wb) {
    Say '[ERROR] WorkBuddy.exe not found. Please start WorkBuddy once, then retry.'
    exit 1
}
Say " WorkBuddy.exe : $wb"
Set-Content -Path (Join-Path $cfg 'workbuddy-exe.txt') -Value $wb -Encoding ASCII

# ---------- 2) backup + repoint the logon entry ----------
$backup = Join-Path $cfg 'run-backup.json'
if (-not (Test-Path $backup)) {
    $old = (Get-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue).$RunName
    if ($old) {
        [pscustomobject]@{ name = $RunName; value = $old } |
            ConvertTo-Json | Set-Content -Path $backup -Encoding UTF8
        Say " backed up original logon entry -> $backup"
    } else {
        Say ' note: no original logon entry found (WorkBuddy was not set to auto-start)'
    }
}

Set-ItemProperty -Path $RunKey -Name $RunName -Value ('"' + $launcher + '"')
Say " logon entry now runs : $launcher"

# ---------- 3) daily push task ----------
$times = $PushTimes.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$triggers = @()
foreach ($t in $times) {
    $parts = $t.Split(':')
    $at = (Get-Date).Date.AddHours([int]$parts[0]).AddMinutes([int]$parts[1])
    $triggers += New-ScheduledTaskTrigger -Daily -At $at
    Say " push schedule : daily at $t"
}

$action = New-ScheduledTaskAction -Execute $pushCmd -WorkingDirectory $root
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Settings $set -Description 'Push WorkBuddy assets to OneDrive (read-only snapshot)' `
        -Force | Out-Null
    Say " scheduled task : $TaskName  [registered]"
} catch {
    Say ''
    Say "[WARN] could not register the scheduled task: $($_.Exception.Message)"
    Say '       Right-click install-autostart.cmd -> "Run as administrator" and retry.'
    Say '       (the logon pull is already installed and will work regardless)'
}

Say ''
Say '=================================================='
Say ' Done.'
Say ''
Say '  At logon  : pull runs first, then WorkBuddy opens'
Say "  Daily     : push at $PushTimes"
Say "  Logs      : $cfg\launch.log   /   $cfg\push.log"
Say '  Undo      : double-click uninstall-autostart.cmd'
Say '=================================================='
Say ''
