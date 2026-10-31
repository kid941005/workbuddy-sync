# 移除「空闲窗口自动 pull」计划任务，并关闭正在跑的实例。
# 不删除任何同步数据，只取消自动触发（pull 仍可手动执行）。

$TaskName = 'WorkBuddy-Sync-Guard'

try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Host " task not installed: $TaskName"
    exit 0
}

if ($t.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host " removed scheduled task: $TaskName"
Write-Host " (guard.log / guard-run.log 保留在 %LOCALAPPDATA%\wb-sync\ 下，需要可自行删除)"
