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
    DEFAULT_HEIGHT = 600
    CHANNELS_PER_COLUMN = 10

    def __init__(self, root: tk.Tk, model: BossTrackerModel) -> None:
        self.root = root
        self.model = model
        self.channel_widgets: dict[str, dict[str, Any]] = {}
        self.custom_widgets: dict[str, dict[str, Any]] = {}
        self._closing = False
        self._building = True

        root.title("BOSS 追踪")
        root.minsize(350, 300)
        root.protocol("WM_DELETE_WINDOW", self.close)
        geometry = model.snapshot().get("window_geometry", "")
        match = re.match(r"^\d+x\d+([+-]\d+[+-]\d+)?$", geometry)
        self._saved_position = match.group(1) if match and match.group(1) else ""

        self._build_ui()
        self._rebuild_channels()
        self._rebuild_statistics()
        self._building = False
        self._fit_to_content(use_saved_position=True)
        self._poll()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(outer, text="通用设置", padding=7)
        settings.pack(fill="x")
        interval_row = ttk.Frame(settings)
        interval_row.grid(row=0, column=0, sticky="w")
        ttk.Label(interval_row, text="统一时间间隔（小时）").pack(side="left")
        hours = self.model.snapshot()["universal_interval_hours"]
        self.interval_var = tk.StringVar(value=f"{hours:g}")
        _interval_holder, interval_entry = self._fixed_entry(
            interval_row, self.interval_var
        )
        _interval_holder.pack(side="left", padx=4)
        ttk.Button(
            interval_row, text="应用并重置全部", command=self._apply_interval
        ).pack(side="left")

        channel_row = ttk.Frame(settings)
        channel_row.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(channel_row, text="频道名称").pack(side="left")
        self.channel_name_var = tk.StringVar()
        channel_holder, channel_entry = self._fixed_entry(
            channel_row, self.channel_name_var
        )
        channel_holder.pack(side="left", padx=4)
        channel_entry.bind("<Return>", lambda _event: self._add_channel())
        ttk.Button(
            channel_row, text="添加频道", command=self._add_channel
        ).pack(side="left")
        channel_box = ttk.LabelFrame(outer, text="各频道 BOSS 倒计时", padding=8)
        channel_box.pack(fill="x", pady=(10, 0))
        self.channel_frame = ttk.Frame(channel_box)
        self.channel_frame.pack(fill="x")

        stats = ttk.LabelFrame(outer, text="统计分析", padding=8)
        stats.pack(fill="x", pady=(10, 0))
        self.stats_frame = ttk.Frame(stats)
        self.stats_frame.pack(fill="x")
        stats_buttons = ttk.Frame(stats)
        stats_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(
            stats_buttons, text="添加自定义统计", command=self._add_custom
        ).pack(side="left")
        ttk.Button(
            stats_buttons, text="清空全部数据", command=self._clear_all_data
        ).pack(side="left", padx=(6, 0))

        self.status_var = tk.StringVar(value="所有数据会自动保存。")
        ttk.Label(outer, textvariable=self.status_var).pack(
            fill="x", pady=(8, 0)
        )

    @staticmethod
    def _fixed_entry(
        parent: tk.Misc,
        variable: tk.StringVar,
        width_px: int = 80,
    ) -> tuple[ttk.Frame, ttk.Entry]:
        """Return an editable field constrained to an exact pixel width."""

        holder = ttk.Frame(parent, width=width_px, height=24)
        holder.pack_propagate(False)
        holder.grid_propagate(False)
        entry = ttk.Entry(holder, textvariable=variable)
        entry.place(x=0, y=0, width=width_px, height=24)
        return holder, entry

    @staticmethod
    def _fixed_label(
        parent: tk.Misc,
        text: str,
        width_px: int = 30,
    ) -> tuple[ttk.Frame, ttk.Label]:
        """Return a left-aligned label constrained to an exact pixel width."""

        holder = ttk.Frame(parent, width=width_px, height=24)
        holder.grid_propagate(False)
        label = ttk.Label(holder, text=text, anchor="w")
        label.place(x=0, y=0, width=width_px, height=24)
        return holder, label

    def _fit_to_content(self, *, use_saved_position: bool = False) -> None:
        """Resize the window around all channel columns and statistic rows."""

        if self._building or self._closing:
            return
        self.root.update_idletasks()
        width = max(350, self.root.winfo_reqwidth())
        height = max(300, self.root.winfo_reqheight())
        if use_saved_position:
            position = self._saved_position
        else:
            position = f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
        self.root.geometry(f"{width}x{height}{position}")

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
                text="尚未添加频道。",
            ).grid(row=0, column=0, sticky="w", padx=4, pady=8)
            self._fit_to_content()
            return
        column_count = (len(rows) - 1) // self.CHANNELS_PER_COLUMN + 1
        for index, row in enumerate(rows):
            grid_column = index // self.CHANNELS_PER_COLUMN
            grid_row = index % self.CHANNELS_PER_COLUMN
            line = ttk.Frame(self.channel_frame, padding=(4, 5))
            line.grid(
                row=grid_row,
                column=grid_column,
                padx=(0, 8 if grid_column < column_count - 1 else 0),
                sticky="ew",
            )
            name_holder, _name_label = self._fixed_label(line, row["name"])
            name_holder.grid(row=0, column=0, sticky="w")
            remaining_var = tk.DoubleVar(value=row["remaining"])
            progress = ttk.Scale(
                line,
                from_=0.0,
                to=row["interval"],
                variable=remaining_var,
                length=110,
                command=lambda value, channel_id=row["id"]:
                self._channel_dragged(channel_id, value),
            )
            progress.grid(row=0, column=1, padx=3)
            progress.bind(
                "<ButtonPress-1>",
                lambda _event, channel_id=row["id"]:
                self._channel_drag_start(channel_id),
            )
            progress.bind(
                "<ButtonRelease-1>",
                lambda _event, channel_id=row["id"]:
                self._channel_drag_end(channel_id),
            )
            remaining = ttk.Label(
                line, text=format_seconds(row["remaining"]), width=8
            )
            remaining.grid(row=0, column=2)
            ttk.Button(
                line,
                text="重置",
                width=4,
                command=lambda channel_id=row["id"]: self._reset_channel(channel_id),
            ).grid(row=0, column=3, padx=(3, 2))
            ttk.Button(
                line,
                text="删除",
                width=4,
                command=lambda channel_id=row["id"]: self._delete_channel(channel_id),
            ).grid(row=0, column=4)
            line.columnconfigure(0, weight=1)
            self.channel_widgets[row["id"]] = {
                "progress": progress,
                "remaining": remaining,
                "remaining_var": remaining_var,
                "dragging": False,
            }
        for column in range(column_count):
            self.channel_frame.columnconfigure(column, weight=1)
        self._fit_to_content()

    def _refresh_channels(self) -> None:
        rows = self.model.channel_status()
        if set(self.channel_widgets) != {row["id"] for row in rows}:
            self._rebuild_channels()
            return
        for row in rows:
            widgets = self.channel_widgets[row["id"]]
            widgets["progress"].configure(to=row["interval"])
            if not widgets["dragging"]:
                widgets["remaining_var"].set(row["remaining"])
                widgets["remaining"].configure(
                    text=format_seconds(row["remaining"])
                )

    def _channel_drag_start(self, channel_id: str) -> None:
        widgets = self.channel_widgets.get(channel_id)
        if widgets is not None:
            widgets["dragging"] = True

    def _channel_dragged(self, channel_id: str, value: str) -> None:
        widgets = self.channel_widgets.get(channel_id)
        if widgets is not None:
            widgets["remaining"].configure(text=format_seconds(float(value)))

    def _channel_drag_end(self, channel_id: str) -> None:
        widgets = self.channel_widgets.get(channel_id)
        if widgets is None:
            return
        remaining = float(widgets["remaining_var"].get())
        self.model.set_channel_remaining(channel_id, remaining)
        widgets["dragging"] = False
        self.status_var.set("该频道剩余时间已调整。")

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
            entry_holder, entry = self._fixed_entry(self.stats_frame, variable)
            entry_holder.grid(row=row_index, column=0, sticky="w", pady=3)
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
        self._fit_to_content()

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
