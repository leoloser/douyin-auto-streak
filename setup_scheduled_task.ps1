param(
    [string]$TaskName = "抖音续火花脚本",
    [string]$PythonPath = "",
    [string[]]$RunAt = @("01:00", "14:00", "23:00")
)

$ErrorActionPreference = "Stop"

$projectDir = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
$launcherPath = Join-Path $projectDir "launch_douyin_keep_streak.py"
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "未找到启动脚本：$launcherPath"
}

if (-not $PythonPath) {
    $existingPython = if ($existingTask) { $existingTask.Actions[0].Execute } else { "" }
    if ($existingPython -and (Test-Path -LiteralPath $existingPython)) {
        $PythonPath = $existingPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $PythonPath = $pythonCommand.Source
    }
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).ProviderPath

$arguments = "`"$launcherPath`""
$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $arguments `
    -WorkingDirectory $projectDir

$triggers = foreach ($timeText in $RunAt) {
    $parsedTime = [datetime]::ParseExact(
        $timeText,
        "HH:mm",
        [Globalization.CultureInfo]::InvariantCulture
    )
    New-ScheduledTaskTrigger -Daily -At $parsedTime
}

if ($existingTask) {
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers | Out-Null
    Write-Host "已更新任务计划：$TaskName"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Principal $principal `
        -Settings $settings `
        -Description "抖音网页版自动续火花脚本" | Out-Null
    Write-Host "已创建任务计划：$TaskName"
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Python：$($task.Actions[0].Execute)"
Write-Host "参数：$($task.Actions[0].Arguments)"
Write-Host "工作目录：$($task.Actions[0].WorkingDirectory)"
Write-Host "下次运行：$($info.NextRunTime)"
