# Maple 助手 — 安装与分辨率指南

## 1. 安装（给最终用户）

项目可以一键自举安装，无需任何 Python 知识。

1. 解压发布压缩包。
2. 右键 **`安装.ps1`** → 使用 PowerShell 运行
   （或在命令行执行 `powershell -ExecutionPolicy Bypass -File 安装.ps1`）。
   - 脚本会查找本机已有的 Python 3.10–3.12（优先使用你的环境）；
     找不到时会**自动下载并安装 Python 3.12**（先尝试 winget，失败则从
     python.org 静默安装）。
   - 创建本地 `.venv` 并安装依赖（numpy、Pillow、OpenCV、pywin32）。
   - 自动生成两个启动器：**`start_assistant.bat`** 和 **`启动助手.bat`**。
3. 可选：YOLO 怪物检测（需下载数 GB 的 PyTorch）：
   `powershell -ExecutionPolicy Bypass -File 安装.ps1 -Yolo`
   - 创建 `yolo-detection\venv313`（界面期望的固定路径）并安装
     torch/ultralytics。
   - 把训练好的模型放到 `yolo-detection\weights\best.pt`。
4. 双击 **`启动助手.bat`** 启动。游戏窗口在前台时点击 **开始巡逻** 即可。

> **不安装 YOLO 也能正常使用**：使用「固定攻击」面板即可普通挂机。
> `-Yolo` 仅用于 AI 怪物检测。

## 2. 支持的分辨率

所有分析都**以游戏客户区为基准归一化**，因此会随你游玩的分辨率自动适配：

| 分辨率 | 比例 | 支持情况 |
|---|---|---|
| 2560×1440 | 16:9 | ✓ 支持 |
| 1920×1080 | 16:9 | ✓ 支持 |
| 1366×768 | 16:9 | ✓ 支持 |
| 2560×1600 | 16:10 | ✓ 支持（原始校准分辨率） |

原理：

- 截图改为抓取**整个客户区**，所有分析区域（小地图、HP/MP 条）都是客户区的
  归一化比例。小地图通过 OpenCV 轮廓动态定位，巡逻点以菱形相对偏移存储，
  HP/MP 条宽度以客户区宽度为基准——因此都与你的分辨率无关。
- YOLO 检测器使用**像素**截图区域（`yolo-detection\config.yaml` →
  `window.default`）和像素 `--attack-range`。请在 YOLO 面板中针对你的分辨率
  设置一次（会保存到 `yolo_detection_settings.json` / `config.yaml`）。

**每个分辨率只需做一次**（切换分辨率会改变 UI 缩放比例）：

1. 确认小地图能被检测到（界面上会显示小地图预览和方框）。
2. 确认状态栏里的 HP/MP 数值正常；如果偏差约 10%，请在
   `status_worker.py` 中调整条宽比例
   （`full_bar_width_fraction`，取值 = 满血条像素 ÷ 客户区宽度）。
3. 如果重新录制巡逻点，请在**将要使用的分辨率下录制**——菱形相对存储让
   坐标可以跨分辨率使用，但在目标分辨率下重新录制最准确。

## 3. 常见问题

- **提示缺少模块 "No module named X"** — 虚拟环境不完整：重新运行 `安装.ps1`。
- **开始巡逻时游戏窗口选择失败** — 助手不会发送 Alt 键（Alt 是跳跃键），
  聚焦依靠直接 `SetForegroundWindow`；请在游戏窗口可见时点击开始巡逻。
- **开始巡逻时角色会跳一下** — 此问题已修复（窗口选择不再按 Alt + 开始
  缓冲期）；若仍出现，请检查 `patrol_start_grace_seconds` / `alt_transition`
  设置。
- **YOLO 提示缺少虚拟环境** — 运行 `安装.ps1 -Yolo`。

## 4. 安全声明

自动化可能违反游戏服务器的规则，请仅在允许的场合使用。
