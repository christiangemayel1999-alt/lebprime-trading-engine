# Windows Scheduled Task installer for MT5, the trading bot, and the dashboard.
# Update the variables below before running the script in an elevated PowerShell session.

$ProjectRoot = "C:\TradingBot"
$Mt5TerminalPath = "C:\Program Files\MetaTrader 5\terminal64.exe"
$BotStartScript = Join-Path $ProjectRoot "scripts\start_bot.bat"
$DashboardStartScript = Join-Path $ProjectRoot "scripts\start_dashboard.bat"
$WorkingUser = $env:USERNAME

$Mt5TaskName = "MT5 Terminal Startup"
$BotTaskName = "MT5 Trading Bot Startup"
$DashboardTaskName = "MT5 Dashboard Startup"

function Register-OrReplaceTask {
    param (
        [string]$TaskName,
        [System.Management.Automation.ActionPreference]$ErrorActionPreference = "Stop",
        [Microsoft.Management.Infrastructure.CimInstance]$Action,
        [Microsoft.Management.Infrastructure.CimInstance]$Trigger,
        [Microsoft.Management.Infrastructure.CimInstance]$Settings
    )

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -User $WorkingUser `
        -RunLevel Highest
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$mt5Trigger = New-ScheduledTaskTrigger -AtLogOn
$mt5Action = New-ScheduledTaskAction -Execute $Mt5TerminalPath
Register-OrReplaceTask -TaskName $Mt5TaskName -Action $mt5Action -Trigger $mt5Trigger -Settings $settings

$botTrigger = New-ScheduledTaskTrigger -AtLogOn
$botTrigger.Delay = "PT45S"
$botAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BotStartScript`""
Register-OrReplaceTask -TaskName $BotTaskName -Action $botAction -Trigger $botTrigger -Settings $settings

$dashboardTrigger = New-ScheduledTaskTrigger -AtLogOn
$dashboardTrigger.Delay = "PT60S"
$dashboardAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$DashboardStartScript`""
Register-OrReplaceTask -TaskName $DashboardTaskName -Action $dashboardAction -Trigger $dashboardTrigger -Settings $settings

Write-Host "Scheduled tasks installed successfully."
