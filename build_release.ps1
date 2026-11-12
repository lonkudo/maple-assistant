<#
.SYNOPSIS
    打包 MoonEclipse 的最小发布文件夹。

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
    $OutDir = "release\MoonEclipse"
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
                   "config.json", "user_config.json", "license.json", "auth.json",
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
                   "README.md", "ARCHITECTURE.md",
                   # 部署地址表只随编译产物 endpoints_protected.pyd 分发（见下），
                   # 源码版不随包发布，否则 IP/端口 会以明文出现在用户目录里。
                   "endpoints_protected.py",
                   # 厂商参考样例（不是运行时文件）会明文打印部署地址。
                   "ct_lie3_remote_workflow_sample.py")
}
foreach ($f in $rootFiles) { Copy-Item $f.FullName $out }
# The live endpoint table ships COMPILED: Nuitka --module produces a .pyd, so the
# package carries no readable address list.  auto_lie_backend imports this module,
# so a build without it cannot start - fail loudly instead of shipping a package
# that raises at import time.
$endpointsSource = Join-Path $root "endpoints_protected.py"
$endpointsDir = Join-Path $root "work\endpoints-pyd"
$endpointsPyd = @(Get-ChildItem -LiteralPath $endpointsDir -File -Filter "endpoints_protected*.pyd" -ErrorAction SilentlyContinue) |
    Sort-Object -Property Name | Select-Object -First 1
# The cache in work\endpoints-pyd is only a build accelerant.  Treating its mere
# existence as "up to date" shipped a stale table once: the 2026-11-09 .pyd (built
# while 222.215.232.230 was still live) was copied into every later package, so the
# retired host stayed a live endpoint behind a comment-only edit of the source.
# Recompile whenever the source is newer than the cached module.
$endpointsStale = $endpointsPyd -and (Test-Path -LiteralPath $endpointsSource) -and
    ((Get-Item -LiteralPath $endpointsSource).LastWriteTimeUtc -gt $endpointsPyd.LastWriteTimeUtc)
if ((-not $endpointsPyd -or $endpointsStale) -and (Test-Path -LiteralPath $endpointsSource)) {
    if ($endpointsStale) {
        Write-Host "endpoints_protected.py is newer than the cached .pyd; recompiling the endpoint table" -ForegroundColor Yellow
    }
    Write-Host "building the compiled endpoint table (endpoints_protected.pyd)..." -ForegroundColor Cyan
    & $python -m nuitka --module $endpointsSource --output-dir=$endpointsDir 2>&1 |
        Select-Object -Last 2
    $endpointsPyd = @(Get-ChildItem -LiteralPath $endpointsDir -File -Filter "endpoints_protected*.pyd" -ErrorAction SilentlyContinue) |
        Sort-Object -Property Name | Select-Object -First 1
}
if ($endpointsPyd) {
    Copy-Item -LiteralPath $endpointsPyd.FullName -Destination (Join-Path $out $endpointsPyd.Name) -Force
    Write-Host "  endpoints: compiled $($endpointsPyd.Name) (no plaintext address list)" -ForegroundColor Cyan
} else {
    throw "endpoints_protected.pyd is missing: the release would ship a plaintext endpoint table or fail to start."
}
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
rem Start the hidden, elevated launcher and close this console immediately.
rem Waiting for wscript.exe kept a cmd window on screen around the UAC prompt,
rem so users read it as something harmful.  "start" detaches the launcher: this
rem window is gone before the UAC confirmation appears.
start "" wscript.exe //nologo "%~dp0launch_assistant.vbs" %*
exit /b
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
    MsgBox "MoonEclipse could not start. Open assistant-launch-status.log in this folder.", 16, "MoonEclipse"
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

# --- 助手配置（默认配置）-----------------------------------------------------
# Shipped so a fresh install starts with the operator's default configuration
# tree.  The installer treats 助手配置 as user data: it writes these files only
# when they are missing and never overwrites them on update.  auth.json is the
# operator's own account token, so it is never published.
$configIn = Join-Path $root "助手配置"
if (Test-Path $configIn) {
    $configRoot = Join-Path $out "助手配置"
    New-Item -ItemType Directory -Force -Path $configRoot | Out-Null
    # 助手配置 is intentionally the portable configuration/export boundary.
    # Keep generated image caches and private authorization out of releases by
    # shipping JSON recording/config documents only.
    #
    # user_config.json is the CLIENT'S OWN settings document (patrol route,
    # recording layers, calibration).  It must never travel inside a package:
    # every install path copies the package over the install root, so a shipped
    # copy replaces the user's configuration on update.  The app writes its
    # defaults on first run instead, and 助手配置 stays user data.
    Get-ChildItem -LiteralPath $configIn -Recurse -File | Where-Object {
        $_.Extension -ieq ".json" -and $_.Name -ne "auth.json" -and
        $_.Name -inotmatch '^user_config\.json$'
    } | ForEach-Object {
        $relative = $_.FullName.Substring($configIn.Length + 1)
        $target = Join-Path $configRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
    $configFiles = @(Get-ChildItem -LiteralPath $configRoot -Recurse -File)
    Write-Host "  助手配置: $($configFiles.Count) 个默认配置文件" -ForegroundColor Cyan
}

# yolo-detection is intentionally not packaged: the live detection feature is
# disabled in the UI, and its model weights (17.5 MB) were dead weight in every
# download.  Enable the feature again before re-adding this folder.

# --- recording-assets（地图识别参考图）--------------------------------------------
$assetsIn = Join-Path $root "recording-assets"
if (Test-Path $assetsIn) {
    Copy-Item $assetsIn (Join-Path $out "recording-assets") -Recurse
}
# --- teach-assets（教学图片）----------------------------------------------------
# Teaching pages are intentionally data-only: UiWorker opens just one page at
# a time.  Ship the source folder unchanged so its runtime path is stable.
$teachIn = Join-Path $root "teach-assets"
if (Test-Path $teachIn) {
    Copy-Item $teachIn (Join-Path $out "teach-assets") -Recurse
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
# （ct_lie3_remote_workflow_sample.py / 2.7.0.md / ip_port.txt）不得改动。2.7.0.md
# is a vendor protocol reference, not runtime data, so it remains read-only in
# the repository but is intentionally excluded from end-user releases.
$apiIn = Join-Path $root "autolie_api"
if (Test-Path $apiIn) {
    $apiOut = Join-Path $out "autolie_api"
    New-Item -ItemType Directory -Path $apiOut -Force | Out-Null
    Get-ChildItem $apiIn -Recurse -File | Where-Object {
        $_.Name -notlike "test_*" -and
        # The application receives the upstream credential through its
        # validated licensing heartbeat.  Never ship the vendor's local
        # key file in a release, even though vendor source remains read-only.
        $_.Name -ne "key_secret.txt" -and
        $_.Name -ne "2.7.0.md" -and
        # ip_port.txt is the vendor's own PLAINTEXT address list.  End users must
        # not be able to read the deployment's IP/port table, so it is never
        # packaged: endpoints_protected.py owns the table and ships compiled
        # (endpoints_protected.pyd), and the vendor loader falls back to its
        # built-in defaults if it ever wants a file.
        $_.Name -ne "ip_port.txt" -and
        # Vendor reference samples are not runtime data and they print the
        # deployment address in plaintext; a test asserts their content in the
        # repository, which is where they stay.
        $_.Name -ne "ct_lie3_remote_workflow_sample.py" -and
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

# --- 本地测谎服务（CT 同行坐标服务）-------------------------------------------
# 本地测谎 要连接 127.0.0.1:8765 的同行坐标服务。程序启动时会自己拉起它（见
# local_lie_service.py），所以服务随包发布到 CT_Lie3_KR_partner_api_DML\，与程序
# 文件同级。只打运行必需的文件（启动器、TaskBroker*.exe、_internal、模式模板、
# data\ct_login_config.json）；日志、license、test_files 与旧的 AppHost*.exe 不打。
$localServicePackager = Join-Path $root "package_local_service.ps1"
if (Test-Path -LiteralPath $localServicePackager) {
    & $localServicePackager -Destination $out
} else {
    Write-Warning "package_local_service.ps1 缺失：本地测谎服务不会随包发布。"
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
    $zipPath = Join-Path $root "release\MoonEclipse-$Version.zip"
    # 仅替换同版本的压缩包；历史发布包保留。
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    }
    Compress-Archive -Path $out -DestinationPath $zipPath -CompressionLevel Optimal -Force
    $zipInfo = Get-Item -LiteralPath $zipPath
    Write-Host "已压缩到 $zipPath ($([math]::Round($zipInfo.Length / 1MB, 1)) MB, $($zipInfo.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')))" -ForegroundColor Green
}
