<#
.SYNOPSIS
    打包 TodoHelper 的最小发布文件夹。

.DESCRIPTION
    只复制运行所需的文件（不含虚拟环境、日志、测试、work 数据、git 元数据）。
    打包结果可压缩后交给其他用户，对方解压后双击 安装.bat 即可自动安装
    Python 与当前启用的依赖（YOLO 怪物检测暂时停用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_release.ps1
    powershell -ExecutionPolicy Bypass -File build_release.ps1 -Zip
#>
param(
    [string]$Version = "",
    [string]$OutDir = "",
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
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "版本号必须是 1.0.0 这样的三段数字（主.次.补丁）: $Version"
}
if (-not $OutDir) {
    $OutDir = "release\TodoHelper"
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
    $name -notin @("launch_assistant_elevated.vbs",
                   "build_release.ps1", "ui_window_settings.json",
                   "COMMIT_MSG.txt", "release_now.ps1", "发布.bat",
                   "build_protected_release.ps1",
                   # User configuration is generated/migrated as
                   # user_config.json and must never be overwritten by an
                   # application update. system_config.json is intentionally
                   # included so internal calibration follows each version.
                   "config.json", "user_config.json", "license.json",
                   "release_no_trade.ps1",
                   "trade_config.json",
                   "recording-configuration.json",
                   # 每机校准文件（trade_offset_probe.py 生成），不随包分发。
                   "trade_offsets.json",
                   "rope_calibration.json", "drug_settings.json",
                   "fixed_attack_settings.json",
                   "additional_functions_settings.json",
                   "yolo_detection_settings.json",
                   # 文档只留在仓库里：发布版不显示 README.md / ARCHITECTURE.md。
                   "README.md", "ARCHITECTURE.md")
}
foreach ($f in $rootFiles) { Copy-Item $f.FullName $out }
Copy-Item -LiteralPath $versionFile -Destination (Join-Path $out "VERSION")
# A version overlay must not replace the package launcher with the development launcher: write the
# launcher into the package so an update keeps using the installed .venv.  There is one environment
# only — the remote API means no local model, so no CPU/CUDA choice.
$environmentDir = ".venv"
$packageBat = @"
@echo off
cd /d "%~dp0"
if not exist "$environmentDir\Scripts\pythonw.exe" (
    echo Package environment missing. Run the installer once.
    pause
    exit /b 1
)
wscript.exe //nologo "%~dp0launch_assistant.vbs" %*
"@
$packageVbs = @"
Option Explicit

Dim shellApp, files, root, pythonwPath, restartScript, arguments, statusPath
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = root & "\$environmentDir\Scripts\pythonw.exe"
restartScript = root & "\restart_assistant.ps1"
statusPath = root & "\assistant-launch-status.log"
arguments = "-NoProfile -ExecutionPolicy Bypass -File " & QuoteArgument(restartScript) & " -Root " & QuoteArgument(root)

Set shellApp = CreateObject("Shell.Application")
WriteStatus "Launcher requested a hidden assistant restart."
On Error Resume Next
shellApp.ShellExecute "powershell.exe", arguments, root, "runas", 0
If Err.Number <> 0 Then
    WriteStatus "Windows could not start the assistant: " & Err.Description
    MsgBox "TodoHelper could not start. Open assistant-launch-status.log in this folder.", 16, "TodoHelper"
End If
On Error GoTo 0

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

Sub WriteStatus(message)
    Dim logFile
    Set logFile = files.OpenTextFile(statusPath, 8, True, 0)
    logFile.WriteLine Now & " " & message
    logFile.Close
End Sub
"@
[System.IO.File]::WriteAllText(
    (Join-Path $out "start_assistant.bat"), $packageBat,
    [System.Text.Encoding]::ASCII
)
[System.IO.File]::WriteAllText(
    (Join-Path $out "启动助手.bat"), $packageBat,
    [System.Text.Encoding]::ASCII
)
[System.IO.File]::WriteAllText(
    (Join-Path $out "launch_assistant.vbs"), $packageVbs,
    [System.Text.Encoding]::ASCII
)

# --- countdown reminder sound -----------------------------------------------
$soundIn = Join-Path $root "sound"
if (Test-Path $soundIn) {
    Copy-Item $soundIn (Join-Path $out "sound") -Recurse
}

# --- detect_video：不再随包分发 ---------------------------------------------------
# 测试测谎用的演示视频留在仓库里（"don't package up test video"）。测试api 由用户自己
# 选择视频文件，运行期不需要这个目录，发布包因此保持精简。

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
# 自动重连 的登录页颜色参考图：screenshots 是本机个人目录，不随包分发，所以参考图
# 也放进 recording-assets，安装后的副本才有颜色参考（reconnect_worker 会依次查找
# screenshots\login_page_target.jpg 和 recording-assets\login_page_target.jpg）。
$loginReferenceIn = Join-Path $root "screenshots\login_page_target.jpg"
if (Test-Path $loginReferenceIn) {
    Copy-Item $loginReferenceIn (Join-Path $out "recording-assets\login_page_target.jpg") -Force
}
# The auto-reconnect workflow identifies the offline dialog from this supplied
# colour patch before clicking its 确定 button. Keep the original PNG because
# its exact sampled colours are the detection reference.
$offlinePromptSquareIn = Join-Path $root "screenshots\offline_prompt_square.png"
if (Test-Path $offlinePromptSquareIn) {
    Copy-Item $offlinePromptSquareIn (Join-Path $out "recording-assets\offline_prompt_square.png") -Force
}

# Auto reconnect cannot safely decide that an absent yellow marker is an
# offline screen without the shipped login-page colour crop.  Fail the build
# here instead of producing an apparently healthy package that can never
# reconnect on an installed machine.
$requiredReconnectAssets = @(
    "login_page_target.jpg"
)
foreach ($assetName in $requiredReconnectAssets) {
    $shippedAsset = Join-Path $out ("recording-assets\" + $assetName)
    if (-not (Test-Path -LiteralPath $shippedAsset -PathType Leaf)) {
        throw "Release package is missing required auto-reconnect asset: $shippedAsset"
    }
}

# --- autolie_api（自动过测谎 API 集成：图像转换 + 坐标换算）------------------------
# 该目录是一个 Python 包，必须随包分发，否则 import autolie_api 会失败。除本机测试
# 文件与体积较大的 sample_captures 外，整个目录原样复制——厂商参考材料
# （ct_lie3_remote_workflow_sample.py / 2.7.0.md / ip_port.txt）不得改动，也不为了
# 打包而删改其中任何文件，见 README.md 的“Reference material is read-only”。
$apiIn = Join-Path $root "autolie_api"
if (Test-Path $apiIn) {
    $apiOut = Join-Path $out "autolie_api"
    New-Item -ItemType Directory -Path $apiOut -Force | Out-Null
    Get-ChildItem $apiIn -Recurse -File | Where-Object {
        $_.Name -notlike "test_*" -and
        $_.FullName -notlike "*sample_captures*" -and
        $_.FullName -notlike "*__pycache__*" -and
        $_.Extension -ne ".pyc"
    } | ForEach-Object {
        $relative = $_.FullName.Substring($apiIn.Length).TrimStart('\')
        $target = Join-Path $apiOut $relative
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item $_.FullName $target -Force
    }
}

# --- target_tracker（只保留鼠标瞄准控制器）----------------------------------------
# 自动过测谎 已删除（测谎改由远端 RoiTrack 服务完成，见 api_lie_video.py），本地
# Cutie 追踪引擎、离线权重（offline_bundle，约 437MB）与视频追踪脚本都不再随包分发。
# 仍然需要的是 target_tracker\mouse_aim_controller.py：测试api 的鼠标瞄准用它，
# 它自己只依赖标准库。
$ttIn = Join-Path $root "target_tracker"
$aimIn = Join-Path $ttIn "mouse_aim_controller.py"
if (Test-Path $aimIn) {
    $ttOut = Join-Path $out "target_tracker"
    New-Item -ItemType Directory -Path $ttOut -Force | Out-Null
    Copy-Item $aimIn $ttOut -Force
    Copy-Item (Join-Path $ttIn "README.md") $ttOut -ErrorAction SilentlyContinue
}

# --- 汇总 ----------------------------------------------------------------------
$files = Get-ChildItem $out -Recurse -File
$totalMB = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "已复制 $($files.Count) 个文件 ($totalMB MB)。" -ForegroundColor Green
Write-Host ""
Write-Host "发布方法:"
Write-Host "  1. 将 $OutDir 文件夹压缩为 zip"
Write-Host "  2. 接收方解压后双击 安装.bat 即可安装运行环境（YOLO 暂停）"
Write-Host "  3. 安装完成后双击 启动助手.bat 开始。" -ForegroundColor Cyan
Write-Host ""

if ($Zip) {
    $zipPath = Join-Path $root "release\TodoHelper-$Version.zip"
    # 仅替换同版本的压缩包；历史发布包保留。
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    }
    Compress-Archive -Path $out -DestinationPath $zipPath -CompressionLevel Optimal -Force
    $zipInfo = Get-Item -LiteralPath $zipPath
    Write-Host "已压缩到 $zipPath ($([math]::Round($zipInfo.Length / 1MB, 1)) MB, $($zipInfo.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')))" -ForegroundColor Green
}
