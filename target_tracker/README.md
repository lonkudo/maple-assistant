# target_tracker

这个目录现在只剩 **`mouse_aim_controller.py`**。

## 为什么

`自动过测谎`（本地 Cutie/渐隐方块追踪）已经删除：测谎改由远端 RoiTrack 服务完成，
见 `api_lie_video.py`（`测试api` 按钮）和 `autolie_api/`。随之删除的还有本地追踪引擎
（`realtime_fade_tracker`、`adaptive_target_tracker.py`、`roi_video_tracker.py`、
`video_player_tracker.py`）与离线权重 `offline_bundle/`（约 437MB），安装脚本也不再
安装 torch/Cutie（`install.ps1` 顶部的 `$InstallLocalTracker = $false`）。

## 留下的文件

`mouse_aim_controller.py` 是鼠标瞄准控制器：只依赖 Python 标准库（ctypes），负责把目标点
写进一个矩形区域并平滑移动真实光标。`测试api` 的视频演练用它把光标限制在视频画面内
（`set_region` + `push_target`）；`api_lie_video.py` 会先把本目录加入 `sys.path` 再导入它。
