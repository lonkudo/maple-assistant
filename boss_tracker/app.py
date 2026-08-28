"""Standalone Tk application for per-channel BOSS countdown tracking."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from audio import play_mp3_async
from model import BossTrackerModel


APP_DIR = Path(__file__).resolve().parent


def format_seconds(seconds: float) -> str:
    total = max(0, int(seconds + 0.5))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class BossTrackerApp:
    POLL_MS = 200
    COMPACT_WIDTH = 350
    DEFAULT_HEIGHT = 600

    def __init__(self, root: tk.Tk, model: BossTrackerModel) -> None:
        self.root = root
        self.model = model
        self.channel_widgets: dict[str, dict[str, Any]] = {}
        self.custom_widgets: dict[str, dict[str, Any]] = {}
        self._closing = False

        root.title("BOSS 追踪")
        root.minsize(330, 500)
        root.protocol("WM_DELETE_WINDOW", self.close)
        geometry = model.snapshot().get("window_geometry", "")
        match = re.match(r"^\d+x(\d+)([+-]\d+[+-]\d+)?$", geometry)
        height = max(500, int(match.group(1))) if match else self.DEFAULT_HEIGHT
        position = match.group(2) if match and match.group(2) else ""
        root.geometry(f"{self.COMPACT_WIDTH}x{height}{position}")

        self._build_ui()
        self._rebuild_channels()
        self._rebuild_statistics()
        self._poll()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(outer, text="通用设置", padding=7)
        settings.pack(fill="x")
        ttk.Label(settings, text="统一时间间隔（小时）").grid(
            row=0, column=0, sticky="w"
        )
        hours = self.model.snapshot()["universal_interval_hours"]
        self.interval_var = tk.StringVar(value=f"{hours:g}")
        ttk.Entry(settings, textvariable=self.interval_var, width=6).grid(
            row=0, column=1, padx=5
        )
        ttk.Button(settings, text="应用并重置全部", command=self._apply_interval).grid(
            row=0, column=2, sticky="w"
        )

        ttk.Label(settings, text="频道名称").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        self.channel_name_var = tk.StringVar()
        channel_entry = ttk.Entry(
            settings, textvariable=self.channel_name_var, width=11
        )
        channel_entry.grid(row=1, column=1, padx=5, pady=(8, 0), sticky="ew")
        channel_entry.bind("<Return>", lambda _event: self._add_channel())
        ttk.Button(settings, text="添加频道", command=self._add_channel).grid(
            row=1, column=2, pady=(8, 0), sticky="w"
        )
        ttk.Button(
            settings, text="清空全部数据", command=self._clear_all_data
        ).grid(row=2, column=0, columnspan=3, pady=(8, 0), sticky="e")
        settings.columnconfigure(1, weight=1)

        channel_box = ttk.LabelFrame(outer, text="各频道 BOSS 倒计时", padding=8)
        channel_box.pack(fill="both", expand=True, pady=(10, 0))
        self.channel_canvas = tk.Canvas(
            channel_box, width=1, height=210, highlightthickness=0
        )
        channel_scroll = ttk.Scrollbar(
            channel_box, orient="vertical", command=self.channel_canvas.yview
        )
        self.channel_frame = ttk.Frame(self.channel_canvas)
        self.channel_window = self.channel_canvas.create_window(
            (0, 0), window=self.channel_frame, anchor="nw"
        )
        self.channel_canvas.configure(yscrollcommand=channel_scroll.set)
        self.channel_canvas.pack(side="left", fill="both", expand=True)
        channel_scroll.pack(side="right", fill="y")
        self.channel_frame.bind("<Configure>", self._sync_channel_scroll)
        self.channel_canvas.bind("<Configure>", self._sync_channel_width)

        stats = ttk.LabelFrame(outer, text="统计分析", padding=8)
        stats.pack(fill="x", pady=(10, 0))
        self.stats_frame = ttk.Frame(stats)
        self.stats_frame.pack(fill="x")
        ttk.Button(stats, text="添加自定义统计", command=self._add_custom).pack(
            anchor="w", pady=(8, 0)
        )

        self.status_var = tk.StringVar(value="所有数据会自动保存。")
        ttk.Label(outer, textvariable=self.status_var).pack(
            fill="x", pady=(8, 0)
        )

    def _sync_channel_scroll(self, _event: Any = None) -> None:
        self.channel_canvas.configure(scrollregion=self.channel_canvas.bbox("all"))

    def _sync_channel_width(self, event: Any) -> None:
        self.channel_canvas.itemconfigure(self.channel_window, width=event.width)

    def _apply_interval(self) -> None:
        try:
            hours = float(self.interval_var.get())
            if hours <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("无效时间", "请输入大于 0 的小时数。")
            return
        hours = self.model.set_interval_hours(hours)
        self.interval_var.set(f"{hours:g}")
        self.status_var.set("统一时间间隔已保存，全部频道已重置。")
        self._refresh_channels()

    def _add_channel(self) -> None:
        self.model.add_channel(self.channel_name_var.get())
        self.channel_name_var.set("")
        self._rebuild_channels()

    def _delete_channel(self, channel_id: str) -> None:
        if messagebox.askyesno("删除频道", "确定删除这个频道及其倒计时吗？"):
            self.model.delete_channel(channel_id)
            self._rebuild_channels()

    def _reset_channel(self, channel_id: str) -> None:
        self.model.reset_channel(channel_id)
        self.status_var.set("该频道倒计时已重置。")
        self._refresh_channels()

    def _clear_all_data(self) -> None:
        if not messagebox.askyesno(
            "清空全部数据",
            "确定删除全部频道、BOSS 讨伐数量和自定义统计吗？\n"
            "统一时间间隔会保留。",
        ):
            return
        self.model.clear_all_data()
        self._rebuild_channels()
        self._rebuild_statistics()
        self.status_var.set("频道数据和统计分析数据已全部清空。")

    def _rebuild_channels(self) -> None:
        for child in self.channel_frame.winfo_children():
            child.destroy()
        self.channel_widgets.clear()
        rows = self.model.channel_status()
        if not rows:
            ttk.Label(
                self.channel_frame,
                text="尚未添加频道。请在上方输入名称并点击“添加频道”。",
            ).pack(anchor="w", padx=4, pady=8)
            return
        for row in rows:
            line = ttk.Frame(self.channel_frame, padding=(4, 5))
            line.pack(fill="x")
            ttk.Label(line, text=row["name"], width=8).grid(
                row=0, column=0, sticky="w"
            )
            remaining = ttk.Label(
                line, text=format_seconds(row["remaining"]), width=10
            )
            remaining.grid(row=0, column=1, padx=(4, 0))
            ttk.Button(
                line,
                text="重置",
                width=5,
                command=lambda channel_id=row["id"]: self._reset_channel(channel_id),
            ).grid(row=0, column=2, padx=(5, 2))
            ttk.Button(
                line,
                text="删除",
                width=5,
                command=lambda channel_id=row["id"]: self._delete_channel(channel_id),
            ).grid(row=0, column=3)
            progress = ttk.Progressbar(
                line, maximum=row["interval"], value=row["remaining"]
            )
            progress.grid(
                row=1, column=0, columnspan=4, pady=(5, 0), sticky="ew"
            )
            line.columnconfigure(0, weight=1)
            self.channel_widgets[row["id"]] = {
                "progress": progress,
                "remaining": remaining,
            }

    def _refresh_channels(self) -> None:
        rows = self.model.channel_status()
        if set(self.channel_widgets) != {row["id"] for row in rows}:
            self._rebuild_channels()
            return
        for row in rows:
            widgets = self.channel_widgets[row["id"]]
            widgets["progress"].configure(
                maximum=row["interval"], value=row["remaining"]
            )
            widgets["remaining"].configure(text=format_seconds(row["remaining"]))

    def _change_boss(self, delta: int) -> None:
        value = self.model.change_boss_kills(delta)
        self.boss_count_label.configure(text=str(value))

    def _add_custom(self) -> None:
        self.model.add_custom_stat()
        self._rebuild_statistics()

    def _rename_custom(self, item_id: str, variable: tk.StringVar) -> None:
        self.model.rename_custom_stat(item_id, variable.get())

    def _change_custom(self, item_id: str, delta: int) -> None:
        value = self.model.change_custom_stat(item_id, delta)
        if value is not None and item_id in self.custom_widgets:
            self.custom_widgets[item_id]["count"].configure(text=str(value))

    def _delete_custom(self, item_id: str) -> None:
        self.model.delete_custom_stat(item_id)
        self._rebuild_statistics()

    def _rebuild_statistics(self) -> None:
        for child in self.stats_frame.winfo_children():
            child.destroy()
        self.custom_widgets.clear()
        data = self.model.snapshot()["statistics"]
        self._build_stat_row(
            0,
            "BOSS 讨伐数量",
            data["boss_kills"],
            lambda: self._change_boss(-1),
            lambda: self._change_boss(1),
        )
        self.boss_count_label = self.stats_frame.grid_slaves(row=0, column=1)[0]

        for row_index, item in enumerate(data["custom"], start=1):
            variable = tk.StringVar(value=item["name"])
            entry = ttk.Entry(self.stats_frame, textvariable=variable, width=12)
            entry.grid(row=row_index, column=0, sticky="ew", pady=3)
            entry.bind(
                "<FocusOut>",
                lambda _event, item_id=item["id"], var=variable:
                self._rename_custom(item_id, var),
            )
            count = ttk.Label(
                self.stats_frame, text=str(item["count"]), width=5, anchor="center"
            )
            count.grid(row=row_index, column=1, padx=6)
            ttk.Button(
                self.stats_frame,
                text="−1",
                width=4,
                command=lambda item_id=item["id"]:
                self._change_custom(item_id, -1),
            ).grid(row=row_index, column=2, padx=2)
            ttk.Button(
                self.stats_frame,
                text="+1",
                width=4,
                command=lambda item_id=item["id"]:
                self._change_custom(item_id, 1),
            ).grid(row=row_index, column=3, padx=2)
            ttk.Button(
                self.stats_frame,
                text="删除",
                width=5,
                command=lambda item_id=item["id"]: self._delete_custom(item_id),
            ).grid(row=row_index, column=4, padx=(6, 0))
            self.custom_widgets[item["id"]] = {
                "count": count,
                "name": variable,
            }
        self.stats_frame.columnconfigure(0, weight=1)

    def _build_stat_row(
        self,
        row: int,
        name: str,
        count: int,
        minus_command: Any,
        plus_command: Any,
    ) -> None:
        ttk.Label(self.stats_frame, text=name).grid(row=row, column=0, sticky="w")
        ttk.Label(
            self.stats_frame, text=str(count), width=5, anchor="center"
        ).grid(row=row, column=1, padx=6)
        ttk.Button(
            self.stats_frame, text="−1", width=4, command=minus_command
        ).grid(row=row, column=2, padx=2)
        ttk.Button(
            self.stats_frame, text="+1", width=4, command=plus_command
        ).grid(row=row, column=3, padx=2)

    def _poll(self) -> None:
        if self._closing:
            return
        expired = self.model.advance_expired()
        if expired:
            play_mp3_async(APP_DIR / "sound" / "beep.mp3")
            self.status_var.set("BOSS 刷新提醒：" + "、".join(expired))
        self._refresh_channels()
        self.root.after(self.POLL_MS, self._poll)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        # A name being edited may still own focus when the title-bar close
        # button is pressed, so persist it explicitly instead of relying only
        # on the normal FocusOut binding.
        for item_id, widgets in self.custom_widgets.items():
            self.model.rename_custom_stat(item_id, widgets["name"].get())
        self.model.set_window_geometry(self.root.geometry())
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BOSS 追踪")
    parser.add_argument(
        "--config",
        type=Path,
        default=APP_DIR / "config.json",
        help="persistent BOSS tracker configuration",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = tk.Tk()
    BossTrackerApp(root, BossTrackerModel(args.config))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
