# =====================================================================
#  WorkBuddy auto-sync : uninstaller
#  Restores the original logon entry and removes the push task.
# =====================================================================
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName  = 'WorkBuddy.WorkBuddy'
$TaskName = 'WorkBuddy-Sync-Push'

function Say($m) { Write-Host $m }

$cfg    = Join-Path $env:LOCALAPPDATA 'wb-sync'
$backup = Join-Path $cfg 'run-backup.json'

Say ''
Say '=================================================='
Say ' WorkBuddy auto-sync - uninstall'
Say '=================================================='
Say ''

# ---- restore logon entry ----
if (Test-Path $backup) {
    try {
        $b = Get-Content -Path $backup -Raw | ConvertFrom-Json
        Set-ItemProperty -Path $RunKey -Name $b.name -Value $b.value
        Say " logon entry restored to : $($b.value)"
    } catch {
        Say " [WARN] restore failed: $($_.Exception.Message)"
    }
} else {
    $cur = (Get-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue).$RunName
    $exe = Join-Path $env:LOCALAPPDATA 'Programs\WorkBuddy\WorkBuddy.exe'
    if ($cur -and $cur -like '*wb-launch.cmd*' -and (Test-Path $exe)) {
        Set-ItemProperty -Path $RunKey -Name $RunName -Value $exe
        Say " logon entry restored to : $exe"
    } else {
        Say ' logon entry left unchanged.'
    }
}

# ---- remove scheduled task ----
try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Say " scheduled task removed  : $TaskName"
    } else {
        Say " scheduled task not present: $TaskName"
    }
} catch {
    Say " [WARN] could not remove task: $($_.Exception.Message)"
    Say '        Right-click uninstall-autostart.cmd -> "Run as administrator" and retry.'
}

Say ''
Say ' Undone. WorkBuddy now starts normally; nothing syncs automatically.'
Say ''
