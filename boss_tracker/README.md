# BOSS 追踪

`BOSS 追踪` 是一个与 Maple Assistant 完全独立的 Windows 桌面程序。它不截屏、
不发送游戏按键，也不依赖巡逻功能。

## 功能

- 一处设置所有频道共用的 BOSS 刷新间隔。
- 输入频道名称并添加任意数量的频道。
- 每个频道拥有独立倒计时、进度条、重置按钮和删除按钮。
- 输入框固定为紧凑的 20 px，频道倒计时保持单行显示。
- 倒计时归零时播放 `sound/beep.mp3`，然后仅重置到期的频道。
- `BOSS 讨伐数量` 提供 `+1` 和 `-1`。
- 可添加任意自定义统计项目；名称可直接编辑，每项都有 `+1`、`-1` 和删除。
- `清空全部数据` 经确认后会清除全部频道和统计，但保留统一时间间隔。
- 所有频道、倒计时截止时间、统计和窗口位置自动保存到 `config.json`。

## 启动

双击 `启动BOSS追踪.bat`，或在本目录运行：

```powershell
py -3.10 app.py
```

程序只使用 Python 3.10 自带组件，不需要安装额外依赖。更新版本时不要覆盖
`config.json`；官方发布 ZIP 不包含该文件。

## 发布

在本目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\release_now.ps1
```

脚本会运行测试、重建 `release/BossTracker`、创建新的时间戳 ZIP，并验证 ZIP
不包含用户的 `config.json`。
