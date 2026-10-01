<#
.SYNOPSIS
    Codex Autoheal Bridge - Windows 一键自动配置与启动脚本
.DESCRIPTION
    自动检测系统环境 (Python 3.11+, Node.js, CLIProxyAPI)，
    配置 8318 智能自愈路由网关，设置开机无窗口静默自启，
    并将多模型无缝注入 Codex Desktop 原生下拉列表中。
#>

[CmdletBinding()]
param(
    [string]$DefaultModel = "gpt-5.6-sol",
    [switch]$SkipSync,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Codex Autoheal Bridge (Windows Native Setup)          " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. 检查 Python 运行环境
$PythonCmd = $null
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    $PythonCmd = "py -3"
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
} elseif (Get-Command "python3" -ErrorAction SilentlyContinue) {
    $PythonCmd = "python3"
} else {
    Write-Error "未检测到 Python 运行环境！请前往 https://www.python.org/downloads/ 安装 Python 3.11+ 并勾选 'Add python.exe to PATH'。"
}
Write-Host "[1/5] 检测到 Python 解释器: $PythonCmd" -ForegroundColor Green

# 2. 检查 Node.js 运行环境
if (-not (Get-Command "node" -ErrorAction SilentlyContinue)) {
    Write-Error "未检测到 Node.js！8318 路由网关需要 Node.js 运行环境，请前往 https://nodejs.org/ 安装 LTS 版本。"
}
$NodeVer = & node --version
Write-Host "[2/5] 检测到 Node.js: $NodeVer" -ForegroundColor Green

# 3. 定位 Skill 根目录
$SkillRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $SkillRoot "scripts\bridge.py"))) {
    $SkillRoot = (Get-Item .).FullName
}
$BridgePy = Join-Path $SkillRoot "scripts\bridge.py"

# 4. 配置 Desktop 透明路由网关 (8318) 并安装 Startup 开机自启
Write-Host "[3/5] 正在配置 Windows 8318 智能路由透明网关..." -ForegroundColor Yellow
$ConfigArgs = @(
    "$BridgePy", "configure-desktop",
    "--platform", "windows",
    "--default-model", $DefaultModel,
    "--apply"
)

$ConfigProc = Start-Process -FilePath ($PythonCmd -split " ")[0] -ArgumentList (($PythonCmd -split " ")[1..($PythonCmd.Length)] + $ConfigArgs) -NoNewWindow -Wait -PassThru
if ($ConfigProc.ExitCode -ne 0) {
    Write-Warning "配置网关返回非 0 状态码 ($($ConfigProc.ExitCode))，请检查 config.toml 权限。"
} else {
    Write-Host "      ✓ 8318 路由网关配置完成，开机隐形自启脚本已写入 Startup 目录" -ForegroundColor Green
}

# 5. 同步第三方与本地模型清单
if (-not $SkipSync) {
    Write-Host "[4/5] 正在同步模型目录 (Gemini, Claude, DeepSeek 等)..." -ForegroundColor Yellow
    $SyncArgs = @("$BridgePy", "sync", "--apply")
    $SyncProc = Start-Process -FilePath ($PythonCmd -split " ")[0] -ArgumentList (($PythonCmd -split " ")[1..($PythonCmd.Length)] + $SyncArgs) -NoNewWindow -Wait -PassThru
    if ($SyncProc.ExitCode -eq 0) {
        Write-Host "      ✓ 模型清单同步成功" -ForegroundColor Green
    }
}

# 6. 验证路由服务健康度
Write-Host "[5/5] 检查 8318 路由服务状态..." -ForegroundColor Yellow
Start-Sleep -Seconds 1
try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8318/__codex_bridge_health" -TimeoutSec 3 -ErrorAction SilentlyContinue
    if ($Health.ok) {
        Write-Host "      ✓ 8318 路由运行正常 (模式: $($Health.mode))" -ForegroundColor Green
    }
} catch {
    Write-Host "      ℹ 路由已配置为开机自启。如当前未启动，双击运行 %USERPROFILE%\.config\codex-cli-model-bridge\run-router-hidden.vbs 即可。" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "🎉 Windows 适配安装配置成功！" -ForegroundColor Green
Write-Host "👉 接下来请彻底退出并重启 Codex 客户端 (Alt + F4)，" -ForegroundColor Cyan
Write-Host "   即可在原生模型下拉列表中自由使用所有模型！" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Green
