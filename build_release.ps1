<#
.SYNOPSIS
    打包 Maple 助手的最小发布文件夹。

.DESCRIPTION
    只复制运行所需的文件（不含虚拟环境、日志、测试、work 数据、git 元数据）。
    打包结果可压缩后交给其他用户，对方解压后双击 安装.bat 即可自动安装
    Python 与当前启用的依赖（YOLO 怪物检测暂时停用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_release.ps1
    powershell -ExecutionPolicy Bypass -File build_release.ps1 -Zip
#>
param(
    [string]$OutDir = "release\MapleAssistant",
    [string]$Version = "",
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$versionFile = Join-Path $root "VERSION"
if (-not $Version) {
    if (-not (Test-Path -LiteralPath $versionFile)) {
        throw "VERSION 文件不存在；请通过 release_now.ps1 发布"
    }
    $Version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
}
if ($Version -notmatch '^\d{4}$') {
    throw "版本号必须是 0000 到 9999 的四位数字: $Version"
}
$out = Join-Path $root $OutDir
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out -Force | Out-Null
Write-Host "正在打包发布目录: $out" -ForegroundColor Cyan

# --- 根目录运行文件 ---------------------------------------------------------------
$rootFiles = Get-ChildItem $root -File | Where-Object {
    $name = $_.Name
    ($_.Extension -in ".py", ".json", ".md", ".ps1", ".vbs", ".bat", ".txt") -and
    $name -notlike "test_*" -and $name -ne "auto_system.log" -and
    # 以下为开发工具/本机私有文件（含本机绝对路径或不适合分发的个人设置），
    # 不随发布包分发。
    $name -notin @("restart_assistant.ps1", "launch_assistant_elevated.vbs",
                   "build_release.ps1", "ui_window_settings.json",
                   "COMMIT_MSG.txt", "release_now.ps1", "发布.bat",
                   # User configuration is generated/migrated as config.json
                   # and must never be overwritten by an application update.
                   "config.json", "recording-configuration.json",
                   "rope_calibration.json", "drug_settings.json",
                   "fixed_attack_settings.json",
                   "additional_functions_settings.json",
                   "yolo_detection_settings.json")
}
foreach ($f in $rootFiles) { Copy-Item $f.FullName $out }
Copy-Item -LiteralPath $versionFile -Destination (Join-Path $out "VERSION")

# --- countdown reminder sound -----------------------------------------------
$soundIn = Join-Path $root "sound"
if (Test-Path $soundIn) {
    Copy-Item $soundIn (Join-Path $out "sound") -Recurse
}

# --- yolo-detection -----------------------------------------------------------
$yoloOut = Join-Path $out "yolo-detection"
New-Item -ItemType Directory -Path $yoloOut -Force | Out-Null
Get-ChildItem (Join-Path $root "yolo-detection") -File | Where-Object {
    ($_.Extension -in ".py", ".yaml", ".txt", ".md") -and
    $_.Name -notin @(
        "elevated_result.txt", "work-check.txt", "live_test_decision.py"
    ) -and
    $_.Name -notlike "*.log"
} | ForEach-Object { Copy-Item $_.FullName $yoloOut }
# 训练好的模型权重 (best.pt ~6MB; best.onnx ~12MB 可选)。
$weightsIn = Join-Path $root "yolo-detection\weights"
$weightsOut = Join-Path $yoloOut "weights"
if (Test-Path $weightsIn) {
    New-Item -ItemType Directory -Path $weightsOut -Force | Out-Null
    Copy-Item (Join-Path $weightsIn "best.pt") $weightsOut -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $weightsIn "best.onnx") $weightsOut -ErrorAction SilentlyContinue
}

# --- recording-assets（地图识别参考图）--------------------------------------------
$assetsIn = Join-Path $root "recording-assets"
if (Test-Path $assetsIn) {
    Copy-Item $assetsIn (Join-Path $out "recording-assets") -Recurse
}

# --- 汇总 ----------------------------------------------------------------------
$files = Get-ChildItem $out -Recurse -File
$totalMB = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "已复制 $($files.Count) 个文件 ($totalMB MB)。" -ForegroundColor Green
Write-Host ""
Write-Host "发布方法:"
Write-Host "  1. 将 $OutDir 文件夹压缩为 zip"
Write-Host "  2. 接收方解压后双击 安装.bat 即可自动安装（YOLO 暂停）"
Write-Host "  3. 安装完成后双击 启动助手.bat 开始。" -ForegroundColor Cyan
Write-Host ""

if ($Zip) {
    $zipPath = Join-Path $root "release\MapleAssistant-v$Version.zip"
    # 先删除旧的压缩包（带重试，防止旧包被资源管理器/杀软短暂锁定），
    # 重建后不会残留旧包。
    Get-ChildItem (Join-Path $root "release") -Filter "MapleAssistant*.zip" |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path $out -DestinationPath $zipPath -CompressionLevel Optimal -Force
    $zipInfo = Get-Item -LiteralPath $zipPath
    Write-Host "已压缩到 $zipPath ($([math]::Round($zipInfo.Length / 1MB, 1)) MB, $($zipInfo.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')))" -ForegroundColor Green
}
