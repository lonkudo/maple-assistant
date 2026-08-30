"""Non-blocking Telegram alert delivery for optional assistant alarms."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import queue
import threading
from typing import Any, Callable, Optional
from urllib import parse, request


LOG = logging.getLogger(__name__)
ApiRequest = Callable[[str, dict[str, str]], dict[str, Any]]


def format_alert_message(
    machine_name: str, event_type: str, when: Optional[datetime] = None
) -> str:
    """Build the deliberately compact multi-machine alert text."""

    timestamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    marker = str(machine_name).strip() or "未命名设备"
    return f"{marker} {str(event_type).strip()} 时间 {timestamp}"


def _telegram_api_request(
    token: str, method: str, params: dict[str, str]
) -> dict[str, Any]:
    """Call one Telegram Bot API method with a bounded network timeout."""

    url = f"https://api.telegram.org/bot{token}/{method}"
    payload = parse.urlencode(params).encode("utf-8")
    http_request = request.Request(url, data=payload, method="POST")
    with request.urlopen(http_request, timeout=8.0) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not result.get("ok"):
        description = (
            result.get("description", "Telegram API returned an error")
            if isinstance(result, dict) else "invalid Telegram response"
        )
        raise OSError(str(description))
    return result


class TelegramNotifier(threading.Thread):
    """Verify configuration and deliver alerts away from UI/game workers."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        api_request: Optional[ApiRequest] = None,
    ) -> None:
        super().__init__(name="telegram-notifier-worker", daemon=True)
        self.stop_event = stop_event
        self._api_request = api_request
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._tasks: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=32)
        self._enabled = False
        self._token = ""
        self._chat_id = ""
        self._machine_name = ""
        self._bot_name = ""
        self._status = "消息提醒: 未启用。"

    def _enqueue(self, kind: str, value: str = "") -> None:
        try:
            self._tasks.put_nowait((kind, value))
        except queue.Full:
            LOG.warning("Telegram alert queue is full; dropped %s", kind)
        self._wake.set()

    def configure(
        self, token: str, chat_id: str = "", machine_name: str = ""
    ) -> None:
        """Apply saved settings and asynchronously verify changed values."""

        token = str(token).strip()
        chat_id = str(chat_id).strip()
        machine_name = str(machine_name).strip()
        with self._lock:
            changed = (
                token != self._token or chat_id != self._chat_id
                or machine_name != self._machine_name
            )
            self._token = token
            self._chat_id = chat_id
            self._machine_name = machine_name
            if not token:
                self._bot_name = ""
                self._status = "消息提醒: 未配置 BOT token。"
            elif changed:
                self._status = "消息提醒: 正在验证 BOT 配置..."
        if token and changed:
            self._enqueue("verify")

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            enabled = bool(enabled)
            changed = enabled != self._enabled
            self._enabled = enabled
            token = self._token
            if not self._enabled:
                self._status = "消息提醒: 未启用。"
            elif not token:
                self._status = "消息提醒: 已启用，但未配置 BOT token。"
            else:
                self._status = "消息提醒: 正在验证 BOT 配置..."
        if enabled and token and changed:
            self._enqueue("verify")

    def notify(self, event_type: str) -> None:
        """Queue one event; this call never performs network work or raises."""

        with self._lock:
            enabled = self._enabled
        if enabled:
            self._enqueue("notify", str(event_type))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "configured": bool(self._token and self._chat_id),
                "chat_id": self._chat_id,
                "machine_name": self._machine_name,
                "bot_name": self._bot_name,
                "status": self._status,
            }

    def _call(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            token = self._token
        if not token:
            raise ValueError("BOT token 未配置")
        if self._api_request is not None:
            return self._api_request(method, params)
        return _telegram_api_request(token, method, params)

    @staticmethod
    def _latest_chat_id(updates: Any) -> str:
        if not isinstance(updates, list):
            return ""
        for update in reversed(updates):
            if not isinstance(update, dict):
                continue
            message = update.get("message") or update.get("channel_post")
            chat = message.get("chat") if isinstance(message, dict) else None
            if isinstance(chat, dict) and chat.get("id") is not None:
                return str(chat["id"])
        return ""

    def _verify(self) -> bool:
        me = self._call("getMe", {})
        bot = me.get("result", {})
        bot_name = str(bot.get("username", "")).strip()
        with self._lock:
            chat_id = self._chat_id
        if not chat_id:
            updates = self._call("getUpdates", {"limit": "20", "timeout": "0"})
            chat_id = self._latest_chat_id(updates.get("result"))
        with self._lock:
            self._bot_name = bot_name
            self._chat_id = chat_id
            enabled_text = "已启用" if self._enabled else "未启用"
            if chat_id:
                self._status = (
                    f"消息提醒: 配置正确，{enabled_text}，"
                    f"BOT @{bot_name or 'unknown'}，"
                    f"聊天 {chat_id}。"
                )
            else:
                self._status = (
                    "消息提醒: BOT token 正确；请先在 Telegram 给 BOT "
                    "发送一条消息，再点修改BOT token重新确认。"
                )
        return bool(chat_id)

    def _send(self, event_type: str) -> None:
        with self._lock:
            enabled = self._enabled
            chat_id = self._chat_id
            machine_name = self._machine_name
        if not enabled:
            return
        if not chat_id and not self._verify():
            return
        with self._lock:
            chat_id = self._chat_id
        text = format_alert_message(machine_name, event_type)
        self._call("sendMessage", {"chat_id": chat_id, "text": text})
        with self._lock:
            self._status = f"消息提醒: 最近一次发送成功（{event_type}）。"

    def run(self) -> None:
        LOG.info("telegram notifier started")
        while not self.stop_event.is_set():
            try:
                kind, value = self._tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if kind == "verify":
                    self._verify()
                elif kind == "notify":
                    self._send(value)
            except Exception as exc:
                # Telegram is optional. Bad credentials/network must never
                # terminate this worker, the UI, or any gameplay worker.
                with self._lock:
                    self._status = f"消息提醒: 配置或发送失败 - {exc}"
                LOG.warning("Telegram %s failed: %s", kind, exc)
        LOG.info("telegram notifier stopped")


__all__ = ["TelegramNotifier", "format_alert_message"]
