$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Push-Location $PSScriptRoot
try {
    $alreadyRunning = $false
    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/state' -TimeoutSec 2
        $alreadyRunning = $null -ne $status.status
    } catch { }
    if ($alreadyRunning) {
        Write-Host '工作数字人已运行：http://127.0.0.1:8765'
    } else {
        Write-Host '工作数字人控制台：http://127.0.0.1:8765'
        uv run lark web --port 8765
    }
} finally {
    Pop-Location
}
