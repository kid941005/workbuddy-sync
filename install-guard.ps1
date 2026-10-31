# 安装「空闲窗口自动 pull」计划任务（guard）。
#
# 为什么需要它：
#   pull 要求 WorkBuddy 完全退出才能写库，而人几乎不会为了同步去关它。
#   结果是每台机器各 push 各的、谁都不 pull，多机一致性停在某个时刻不再前进。
#   guard 每 15 分钟被唤醒一次，看到 WorkBuddy 没在跑就 pull，在跑就立刻退出。
#   不需要用户记得任何事 —— 关得越频繁，收敛越及时。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-guard.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install-guard.ps1 -IntervalMinutes 30
#   powershell -NoProfile -ExecutionPolicy Bypass -File uninstall-guard.ps1

param(
    [int]$IntervalMinutes = 15
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$guardCmd = Join-Path $root 'guard.cmd'
$TaskName = 'WorkBuddy-Sync-Guard'

function Say($m) { Write-Host $m }

if (-not (Test-Path $guardCmd)) {
    Say "[X] guard.cmd not found next to this script: $guardCmd"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $guardCmd -WorkingDirectory $root

# -Once + Repetition：比 -Daily 更适合「每 N 分钟」。3650 天当作无限，
# 因为 PS 5.1 不接受 [TimeSpan]::MaxValue。
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description 'Pull WorkBuddy assets when the client is not running (no-op otherwise)' | Out-Null
    Say " scheduled task : $TaskName  [registered, every $IntervalMinutes min]"
} catch {
    Say ''
    Say "[WARN] could not register the task: $($_.Exception.Message)"
    Say '       Right-click this script -> "Run with PowerShell" as administrator, then retry.'
    exit 1
}

Say ''
Say '=================================================='
Say ' guard installed.'
Say '   log    : %LOCALAPPDATA%\wb-sync\guard.log      (台账，每次真正 pull 记一行)'
Say '   console: %LOCALAPPDATA%\wb-sync\guard-run.log  (完整输出，1 MB 轮转)'
Say '   remove : uninstall-guard.ps1'
Say '=================================================='
