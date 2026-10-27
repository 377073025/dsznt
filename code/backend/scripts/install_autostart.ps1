# 开机自启（07 号文档 2.2 节）：注册 Windows 计划任务，登录后启动看门狗（看门狗再拉起 web 与 worker）。
#
# 用法（PowerShell，在 code/backend 目录，用要运行服务的那个 Windows 账号执行）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1          # 安装
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove  # 卸载
#
# 说明：
# - 用「登录时」触发而不是「开机时」：浏览器登录会话和凭据管理器里的密钥都属于当前用户。
#   主机需设置自动登录或保持登录、关闭睡眠（02 号文档运行要求）。
# - 任务本身失败时每分钟重试，最多 999 次；进程级崩溃由看门狗负责在几秒内重启。
param([switch]$Remove, [string]$TaskName = "dsznt-shop-ai")

$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $TaskName"
    exit 0
}

$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "找不到 $python，请先创建虚拟环境并安装依赖" }

$action = New-ScheduledTaskAction -Execute $python -Argument "scripts\watchdog.py" -WorkingDirectory $backend
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "店铺 AI 闭环：看门狗（web + worker）" -Force | Out-Null
Write-Host "已注册计划任务 $TaskName：登录后自动启动 $python scripts\watchdog.py"
Write-Host "立即启动：Start-ScheduledTask -TaskName $TaskName"
