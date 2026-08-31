"""Non-blocking Telegram alert delivery for optional assistant alarms."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import queue
import socket
import threading
from typing import Any, Callable, Optional
from urllib import error as url_error, parse, request


LOG = logging.getLogger(__name__)
ApiRequest = Callable[[str, dict[str, str]], dict[str, Any]]

# Local HTTP proxy ports used by common proxy tools (Clash 7890, Clash
# Verge 7891, Clash Verge Rev / mihomo 7897, V2Ray 10808/10809, Shadowsocks
# 1080, ...).  The Windows system proxy (registry) can point at a STALE port
# after the proxy app restarts or switches profiles, which makes urllib fail
# with cryptic errors (e.g. "urlopen error [Errno 2]").  When the configured
# proxy stops working the notifier probes these ports and caches the first
# one that actually forwards to Telegram.
LOCAL_PROXY_PORTS = (7890, 7891, 7897, 7899, 1080, 10808, 10809, 8888, 8889)

_proxy_state: dict[str, Any] = {"url": None, "verified": False}
_proxy_lock = threading.Lock()


def _system_proxy_urls() -> list[str]:
    """System proxy URLs (Windows registry / env), https then http."""

    proxies = request.getproxies()
    urls: list[str] = []
    for key in ("https", "http"):
        value = str(proxies.get(key) or "").strip()
        if value and value not in urls:
            urls.append(value)
    return urls


def _listening_proxy_ports(timeout: float = 0.25) -> set[int]:
    """Return LOCAL_PROXY_PORTS accepting a TCP connect on 127.0.0.1."""

    listening: set[int] = set()
    for port in LOCAL_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                listening.add(port)
        except OSError:
            continue
    return listening


def _proxy_forwarded(proxy: Optional[str], timeout: float = 3.0) -> bool:
    """True when a getMe probe through ``proxy`` reaches Telegram.

    Any HTTP response (401/404 included) means the proxy forwarded; only
    connection/transport failures count as unusable.  ``None`` probes a
    DIRECT connection (bypassing the system proxy).
    """

    url = f"https://api.telegram.org/bot{'0' * 9}:PROBE/getMe"
    probe = request.Request(url, data=b"", method="POST")
    try:
        with _open(probe, proxy, timeout=timeout):
            return True
    except url_error.HTTPError:
        return True
    except (OSError, url_error.URLError):
        return False


def _open(req: request.Request, proxy: Optional[str], timeout: float):
    """Open ``req`` through ``proxy``, or direct when ``proxy`` is None."""

    if proxy:
        handler = request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = request.ProxyHandler({})  # empty = bypass system proxy
    return request.build_opener(handler).open(req, timeout=timeout)


def _detect_working_proxy() -> Optional[str]:
    """Return the first proxy URL that forwards to Telegram, or None=direct."""

    system = _system_proxy_urls()
    # Fast path: the configured system proxy is usually correct.
    for proxy in system:
        if _proxy_forwarded(proxy):
            LOG.info("telegram proxy: using system proxy %s", proxy)
            return proxy
    # Fallback: scan the common local proxy ports.
    listening = _listening_proxy_ports()
    for port in LOCAL_PROXY_PORTS:
        if port in listening:
            url = f"http://127.0.0.1:{port}"
            if url not in system and _proxy_forwarded(url):
                LOG.info(
                    "telegram proxy: system proxy stale; using %s", url
                )
                return url
    # Last resort: a direct connection may work without any proxy.
    if _proxy_forwarded(None):
        LOG.info("telegram proxy: no proxy needed (direct)")
        return None
    LOG.warning("telegram proxy: no working proxy found")
    return system[0] if system else None


def _cached_proxy() -> Optional[str]:
    """Detect once and cache the working proxy until invalidated."""

    global _proxy_state
    with _proxy_lock:
        if not _proxy_state["verified"]:
            _proxy_state = {"url": _detect_working_proxy(), "verified": True}
        return _proxy_state["url"]


def _invalidate_proxy_cache() -> None:
    """Force the next call to re-detect the proxy."""

    global _proxy_state
    with _proxy_lock:
        _proxy_state["verified"] = False


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
    """Call one Telegram Bot API method with a bounded network timeout.

    Uses the cached working proxy (auto-detected).  A network-layer failure
    invalidates the cache and retries ONCE with a fresh proxy so a proxy app
    that just restarted on a different port does not lose the alert.
    """

    url = f"https://api.telegram.org/bot{token}/{method}"
    payload = parse.urlencode(params).encode("utf-8")
    http_request = request.Request(url, data=payload, method="POST")

    def attempt() -> dict[str, Any]:
        proxy = _cached_proxy()
        try:
            with _open(http_request, proxy, timeout=8.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except url_error.HTTPError:
            raise  # proxy worked; Telegram itself rejected the request
        except (OSError, url_error.URLError) as exc:
            _invalidate_proxy_cache()
            message = (
                "网络代理不可用（Errno 2），已尝试重新检测代理端口。"
                if isinstance(exc, FileNotFoundError) else str(exc)
            )
            raise _NetworkFailure(message) from exc

    try:
        result = attempt()
    except _NetworkFailure:
        result = attempt()  # one retry with a freshly detected proxy
    if not isinstance(result, dict) or not result.get("ok"):
        description = (
            result.get("description", "Telegram API returned an error")
            if isinstance(result, dict) else "invalid Telegram response"
        )
        raise OSError(str(description))
    return result


class _NetworkFailure(OSError):
    """Transport-level failure; safe to retry with a fresh proxy."""


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
