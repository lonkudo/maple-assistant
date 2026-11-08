"""钉钉 (DingTalk) group-robot alerts, optional and non-blocking.

The operator pastes the custom robot's webhook into 附加功能 → 钉钉提醒.  The
robot is configured with the **keyword** requirement, so every message starts
with ``MoonEclipse``: DingTalk rejects a message without the keyword
(``errcode 310000``), which is why the word is added here instead of relying on
each caller to remember it.

The text follows the same compact style as the Telegram alert
(``{设备} {事件} 时间 {时间}``), only prefixed with the keyword.

Nothing in this module touches Tk or a gameplay worker: :func:`send_in_background`
runs the network call on its own short-lived thread and hands the outcome to a
callback, so a slow or unreachable DingTalk server can never stall the assistant.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional
from urllib import error as url_error, request
from urllib.parse import urlsplit


LOG = logging.getLogger(__name__)

# DingTalk 自定义机器人 keyword security: every message must contain this word.
DINGTALK_KEYWORD = "MoonEclipse"
# A webhook answer is a few dozen bytes; the send itself must stay bounded so a
# blocked network never pins the notifier thread.
DINGTALK_TIMEOUT = 8.0
_WEBHOOK_PREFIXES = (
    # Classic group robot (自定义机器人).
    "https://oapi.dingtalk.com/robot/send",
    # Newer robot endpoint.
    "https://api.dingtalk.com/v1.0/robot/",
    # 钉钉连接器 "webhook 触发" flow: https://connector.dingtalk.com/webhook/flow/<id>
    # The operator's own link uses this form, and its body is whatever the flow's
    # trigger declares, so it is a valid target even though it is not a robot URL.
    "https://connector.dingtalk.com/webhook/",
)


class DingTalkError(RuntimeError):
    """A user-readable DingTalk delivery failure."""


def normalize_webhook(value: Any) -> str:
    """Return a cleaned custom-robot webhook URL, or "" when it is unusable.

    Only the group-robot send endpoints are accepted: pasting a message text, a
    share link or an http:// URL by mistake must be caught here, while the
    dialog is still open, instead of silently saving something that can never
    deliver.
    """

    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.lower().startswith("http://"):
        text = "https://" + text[len("http://"):]
    if any(character.isspace() for character in text):
        return ""
    split = urlsplit(text)
    if split.scheme != "https" or not split.netloc:
        return ""
    if not text.lower().startswith(_WEBHOOK_PREFIXES):
        return ""
    return text


def format_dingtalk_message(
    machine_name: str, event_type: str, when: Optional[datetime] = None,
) -> str:
    """Build the compact single-line alert, keyword first.

    Same shape as ``telegram_notifier.format_alert_message`` so one operator
    reading both channels sees the same information: device, what happened,
    when.
    """

    timestamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    marker = str(machine_name).strip() or "未命名设备"
    return (
        f"{DINGTALK_KEYWORD} {marker} {str(event_type).strip()} 时间 {timestamp}"
    )


def _post(webhook: str, payload: bytes) -> tuple[int, str]:
    """POST one payload: direct first, then through the system proxy.

    DingTalk is domestic, so the direct connection is the normal path; the
    proxy retry covers a machine whose only route out is the configured proxy
    (the Telegram notifier has to do the opposite, because Telegram is usually
    only reachable through it).

    Returns ``(status, body)``.  A non-2xx answer is returned rather than
    raised so the caller can explain it (a connector flow answers ``555 webhook
    not found`` as plain text, not as a robot error).
    """

    def attempt(opener: request.OpenerDirector) -> tuple[int, str]:
        http_request = request.Request(
            webhook, data=payload, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with opener.open(http_request, timeout=DINGTALK_TIMEOUT) as response:
                return int(response.status), response.read().decode("utf-8", "replace")
        except url_error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - the status still explains it
                body = ""
            return int(exc.code), body

    try:
        return attempt(request.build_opener(request.ProxyHandler({})))
    except (OSError, url_error.URLError):
        return attempt(request.build_opener())


def _http_hint(status: int, body: str) -> str:
    """Explain a non-2xx answer, including the connector's own wording."""

    detail = (body or "").strip()[:160]
    lowered = detail.casefold()
    if "webhook not found" in lowered:
        return (
            "钉钉返回 webhook not found：这个链接对应的流程不存在或未启用。"
            "请到钉钉连接器里确认流程已发布/启用，再复制最新的 webhook 链接"
            "（调试地址只在调试时可用）"
        )
    if status == 401 or status == 403:
        return f"钉钉拒绝访问（HTTP {status}）：{detail or '无权限'}"
    if status == 429:
        return "发送太频繁：钉钉限流（每分钟 20 条）"
    return f"钉钉返回 HTTP {status}：{detail or '无响应内容'}"


def _judge(status: int, body: str) -> str:
    """Turn one HTTP answer into a status string or a DingTalkError."""

    text = (body or "").strip()
    if status < 200 or status >= 300:
        raise DingTalkError(_http_hint(status, text))
    if not text:
        # Several connector flows answer 200 with an empty body.
        return "ok"
    try:
        parsed = json.loads(text)
    except ValueError:
        # Plain text ("success") from a flow: the HTTP status is the verdict.
        return text[:120] or "ok"
    if not isinstance(parsed, dict):
        return "ok"
    errcode = parsed.get("errcode")
    if errcode not in (None, 0, "0"):
        raise DingTalkError(_errcode_hint(errcode, str(parsed.get("errmsg", ""))))
    if parsed.get("success") is False or parsed.get("success") == "false":
        raise DingTalkError(
            "钉钉流程返回失败：" + str(parsed.get("message") or parsed.get("msg") or text)[:160]
        )
    code = parsed.get("code")
    if code not in (None, 0, "0", "200", "success", "OK", "ok"):
        detail = parsed.get("message") or parsed.get("msg") or text
        raise DingTalkError(f"钉钉流程返回失败（code={code}）：{str(detail)[:160]}")
    return str(parsed.get("errmsg") or parsed.get("message") or "ok")


def _payload_for(url: str, content: str, *, flat: bool) -> bytes:
    """The JSON body for one URL kind.

    A group robot takes ``msgtype`` + ``text.content``.  A 钉钉连接器 flow takes
    whatever its trigger declares, which the caller cannot know, so the robot
    shape is tried first and the flat ``content``/``message`` shape second when
    the answer complains about a parameter.
    """

    if flat:
        body = {
            "content": content,
            "message": content,
            "text": content,
            "title": f"{DINGTALK_KEYWORD} 提醒",
        }
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    if "connector.dingtalk.com" in url:
        # Connector flows usually also accept the robot shape; keep the text
        # available under the flat keys that such a trigger typically maps.
        body = {
            "msgtype": "text",
            "text": {"content": content},
            "content": content,
            "message": content,
        }
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    body = {"msgtype": "text", "text": {"content": content}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def send_dingtalk_text(webhook: str, text: str) -> str:
    """Send one text message; return DingTalk's status or raise DingTalkError."""

    url = normalize_webhook(webhook)
    if not url:
        raise DingTalkError(
            "webhook 链接无效：请粘贴钉钉群机器人 "
            "（https://oapi.dingtalk.com/robot/send?access_token=…）"
            "或钉钉连接器流程（https://connector.dingtalk.com/webhook/…）的完整地址"
        )
    content = str(text or "").strip()
    if DINGTALK_KEYWORD.casefold() not in content.casefold():
        # Keyword security is the operator's chosen mode; never send a message
        # DingTalk is guaranteed to reject.
        content = f"{DINGTALK_KEYWORD} {content}".strip()
    try:
        status, body = _post(url, _payload_for(url, content, flat=False))
    except (OSError, url_error.URLError) as exc:
        raise DingTalkError(f"无法连接钉钉服务器：{exc}") from exc
    try:
        result = _judge(status, body)
    except DingTalkError as first:
        # A connector flow may want a flat body instead of the robot shape.  Only
        # retry when the answer clearly points at the request body, so a genuine
        # rejection (bad token, disabled flow) is reported as it is.
        detail = str(first)
        looks_like_body_problem = any(
            marker in detail.casefold()
            for marker in ("参数", "parameter", "content", "不能为空", "missing",
                           "invalid", "required")
        )
        if not looks_like_body_problem:
            raise
        try:
            status, body = _post(url, _payload_for(url, content, flat=True))
        except (OSError, url_error.URLError) as exc:
            raise DingTalkError(f"无法连接钉钉服务器：{exc}") from exc
        result = _judge(status, body)
        LOG.info("钉钉提醒: the flat body was accepted instead of the robot shape")
    LOG.info("钉钉提醒: message delivered (%s)", result or "ok")
    return result or "ok"


def _errcode_hint(errcode: Any, errmsg: str) -> str:
    try:
        code = int(errcode)
    except (TypeError, ValueError):
        return f"钉钉拒绝发送：errcode={errcode} {errmsg}".strip()
    hints = {
        310000: "钉钉安全校验未通过（关键字/加签/IP 白名单）：消息必须包含 "
                f"{DINGTALK_KEYWORD}，且机器人安全设置要与 webhook 匹配",
        130101: "发送太频繁：钉钉机器人每分钟最多 20 条",
        300001: "webhook 的 access_token 无效，或机器人已被停用/删除",
        # Verified against the live endpoint: a bogus token answers 300005.
        300005: "webhook 的 access_token 不存在或已失效，请复制机器人最新的链接",
    }
    return hints.get(code, f"钉钉拒绝发送：errcode={code} {errmsg}".strip())


def send_in_background(
    webhook: str, text: str, on_done: Callable[[Optional[str], Optional[str]], None],
) -> None:
    """Send without blocking the caller; ``on_done(status, error)`` reports back.

    The callback runs on this short-lived thread, so a UI caller must hand the
    result to Tk through its own queue.
    """

    def work() -> None:
        status: Optional[str] = None
        error: Optional[str] = None
        try:
            status = send_dingtalk_text(webhook, text)
        except DingTalkError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - optional alert channel
            LOG.exception("钉钉提醒: unexpected send failure")
            error = f"钉钉发送失败：{exc}"
        try:
            on_done(status, error)
        except Exception:  # noqa: BLE001 - a result sink must not crash the thread
            LOG.exception("钉钉提醒: result callback failed")

    threading.Thread(target=work, name="dingtalk-send", daemon=True).start()


# The events the operator asked to be told about, with the wording that goes
# into the group.  Keys are the strings the assistant's alert callbacks already
# emit, so no call site has to change to add a channel.
EVENT_LABELS = {
    "掉线": "掉线",
    "测谎": "测谎出现",
    "循环": "循环提醒",
}
# One message per event type per window: a stuck or flapping session must not
# burn the robot's 20-per-minute budget (nor spam the group).
EVENT_COALESCE_SECONDS = 10.0


class DingTalkNotifier(threading.Thread):
    """Deliver 掉线 / 测谎 / 循环 events to one DingTalk webhook.

    The thread owns the queue and the network, so a worker that raises an alert
    never waits for DingTalk (the same contract as ``TelegramNotifier``).  The
    UI only writes the configuration and reads :meth:`status_text`, which the
    图层校准与巡逻 panel shows as its hint.
    """

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="dingtalk-notifier-worker", daemon=True)
        self.stop_event = stop_event
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._tasks: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=32)
        self._webhook = ""
        self._machine_name = ""
        self._enabled = False
        self._status = "未设置"
        self._last_sent: dict[str, float] = {}
        self._last_event = ""

    # ------------------------------------------------------------ settings
    def configure(
        self, webhook: str, machine_name: str = "",
        enabled: Optional[bool] = None,
    ) -> None:
        """Apply the saved settings; ``enabled=None`` keeps the current switch."""

        cleaned = normalize_webhook(webhook)
        with self._lock:
            self._webhook = cleaned
            self._machine_name = str(machine_name or "").strip()
            if enabled is not None:
                self._enabled = bool(enabled)
            self._refresh_status_locked()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._refresh_status_locked()

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def webhook(self) -> str:
        with self._lock:
            return self._webhook

    def status_text(self) -> str:
        with self._lock:
            return self._status

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "configured": bool(self._webhook),
                "webhook": self._webhook,
                "machine_name": self._machine_name,
                "status": self._status,
                "last_event": self._last_event,
            }

    def _refresh_status_locked(self) -> None:
        """One short state for the UI hint; the UI adds the 钉钉消息 prefix.

        The wording deliberately carries no usage instructions: how to configure
        the webhook and which events are pushed belong to the panel tooltips,
        and repeating them here made the same sentence appear twice.
        """

        if not self._webhook:
            self._status = "未设置"
            return
        self._status = "已开启" if self._enabled else "已设置，未开启"

    # --------------------------------------------------------------- events
    def notify(self, event_type: str) -> None:
        """Queue one key event; never blocks and never raises."""

        key = str(event_type or "").strip()
        if not key:
            return
        label = EVENT_LABELS.get(key, key)
        with self._lock:
            if not (self._enabled and self._webhook):
                return
        self._enqueue("event", label)

    def send_test(self, event_label: str = "测试提醒") -> None:
        """Send one test message regardless of the enable switch."""

        with self._lock:
            if not self._webhook:
                return
        self._enqueue("test", str(event_label or "测试提醒"))

    def _enqueue(self, kind: str, value: str) -> None:
        try:
            self._tasks.put_nowait((kind, value))
        except queue.Full:
            LOG.warning("钉钉提醒: queue is full; dropped %s %s", kind, value)
            return
        self._wake.set()

    def _coalesce(self, label: str) -> bool:
        """True when this event must be swallowed (already sent recently)."""

        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(label, 0.0)
            if now - last < EVENT_COALESCE_SECONDS:
                return True
            self._last_sent[label] = now
        return False

    def _deliver(self, kind: str, label: str) -> None:
        with self._lock:
            webhook = self._webhook
            machine_name = self._machine_name
        if not webhook:
            return
        if kind == "event" and self._coalesce(label):
            LOG.info("钉钉提醒: %s already sent within %.0fs; skipped",
                     label, EVENT_COALESCE_SECONDS)
            return
        text = format_dingtalk_message(machine_name, label)
        try:
            send_dingtalk_text(webhook, text)
        except DingTalkError as error:
            with self._lock:
                self._last_event = label
                self._status = f"失败 - {str(error)[:40]}"
            LOG.warning("钉钉提醒: %s could not be delivered - %s", label, error)
            return
        except Exception as error:  # noqa: BLE001 - optional alert channel
            with self._lock:
                self._status = f"失败 - {str(error)[:40]}"
            LOG.exception("钉钉提醒: unexpected failure")
            return
        with self._lock:
            self._last_event = label
            self._status = f"已发送({label})"
        LOG.info("钉钉提醒: delivered %s", label)

    # ----------------------------------------------------------------- thread
    def run(self) -> None:
        LOG.info("dingtalk notifier started")
        while not self.stop_event.is_set():
            try:
                kind, value = self._tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._deliver(kind, value)
            except Exception:  # noqa: BLE001 - keep the worker alive
                LOG.exception("钉钉提醒: delivery loop failed")
        LOG.info("dingtalk notifier stopped")


__all__ = [
    "DINGTALK_KEYWORD", "DINGTALK_TIMEOUT", "EVENT_LABELS", "DingTalkError",
    "DingTalkNotifier", "format_dingtalk_message", "normalize_webhook",
    "send_dingtalk_text", "send_in_background",
]
