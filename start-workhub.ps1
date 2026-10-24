param([switch]$Stop, [switch]$Status)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$locationPushed = $false
$startupLog = $null

function Write-StartupLine {
    param([string]$Text)
    if ($startupLog) {
        [IO.File]::AppendAllText($startupLog, $Text + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    }
    Write-Host $Text
}

function Invoke-LoggedUv {
    param([string[]]$Arguments)
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $uvProgram @Arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-StartupLine $line
            $line
        })
        $uvExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($uvExitCode -ne 0) { throw "工作台守护操作失败，退出码：$uvExitCode" }
    return $lines
}

try {
    $logDirectory = Join-Path $PSScriptRoot 'data\logs\workhub'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $startupLog = Join-Path $logDirectory 'startup.log'
    Write-StartupLine ('[' + (Get-Date -Format o) + '] Workhub launcher')
    Push-Location $PSScriptRoot
    $locationPushed = $true
    $uvProgram = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $action = if ($Stop) { 'stop' } elseif ($Status) { 'status' } else { 'start' }
    $null = Invoke-LoggedUv -Arguments @('run', 'python', '-m', 'larkx.workhub.supervisor', $action)
    if (-not $Stop -and -not $Status) {
        $hubUrl = @(Invoke-LoggedUv -Arguments @('run', 'lark', 'web', '--print-url'))[-1].Trim()
        $ready = $false
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
            try {
                $webState = Invoke-RestMethod -Uri "$hubUrl/api/state" -TimeoutSec 2
                if ($null -ne $webState.status) { $ready = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 200
        } while ([DateTime]::UtcNow -lt $deadline)
        if (-not $ready) { throw "网页服务未就绪，请查看服务日志：data\logs\workhub\service.log" }
        Write-StartupLine "工作数字人控制台：$hubUrl（无窗口后台运行，守护已开启）"
        Write-StartupLine '启动完成，可以关闭此终端窗口。'
        Write-StartupLine '停止服务：.\start-workhub.ps1 -Stop'
    }
} catch {
    Write-StartupLine ('启动操作失败：' + $_.Exception.Message)
    Write-Host '入口日志：程序目录\data\logs\workhub\startup.log'
    throw
} finally {
    if ($locationPushed) { Pop-Location }
}
