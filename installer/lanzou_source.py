"""Resolve a netdisk share link (lanzou 优享/classic, 123云盘) to a direct URL.

The flow is the one qaiu/netdisk-fast-download implements in ``IzTool.java`` +
``AESUtils.java`` + ``AcwScV2Generator.java``; it was reproduced and verified
against a live share (2026-11-05):

  share page  https://www.ilanzou.com/s/<shareKey>[?code=<password>]
      |
      |  1. uuid = 21 random URL-safe chars, ts = enc(now_ms)
      |  2. POST api.ilanzou.com/unproved/buy/vip/list?devType=6&...   (warm-up)
      |  3. POST api.ilanzou.com/unproved/recommend/list?shareId=<key>[&code=]
      |         -> list[0].userId, list[0].fileList[0].{fileId,fileName,fileSize}
      |         ("var arg1='" in the body = acw_sc__v2 anti-bot page: solve, retry once)
      |  4. GET  api.ilanzou.com/unproved/file/redirect
      |         ?downloadId=enc("<fileId>|<userId>")
      |         &timestamp=enc("<now_ms>")&auth=enc("<fileId>|<timestamp>")
      |         (no redirect followed; NO shareId parameter - it makes the API
      |          answer {"msg":"参数缺失","code":-1})
      |         -> 302 Location = https://<cdn-host>/.../file?filename=<name>...
      v
  direct URL (Range-capable, ~4 MB/s per connection, ~19 MB/s at 8 parts)

The share is treated as UNTRUSTED transport: the caller must verify the
downloaded file against the SHA-256 it obtained from the pinned release
manifest, exactly like any other mirror.

No parser service is required - everything below is stdlib + ``cryptography``.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import logging
import os
import random
import re
import ssl
import string
import zlib
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

LOG = logging.getLogger("lanzou")

API_HOST = "api.ilanzou.com"
# AESUtils.generateKey() truncates to 16 chars; this is the recovered
# CIPHER_AES0_IZ plaintext (see module docstring).
AES_KEY = b"lanZouY-disk-app"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.ilanzou.com/"
SHARE_HOSTS = ("ilanzou.com", "www.ilanzou.com")
# Classic lanzou shares: https://wwblt.lanzoup.com/<fileId> (no /s/ segment).
CLASSIC_HOSTS = (
    "lanzoup.com", "lanzoux.com", "lanzoui.com", "lanzouw.com", "lanzou.com",
)
# QQ 邮箱文件中转 share hosts.
QQ_HOSTS = ("mail.qq.com", "wx.mail.qq.com", "iwx.mail.qq.com")
QQ_SHARE_URL = "https://wx.mail.qq.com/s"
# 123云盘 share hosts; every one of them is answered by the same API host.
PAN_HOSTS = (
    "123pan.com", "123684.com", "123865.com", "123912.com", "123pan.cn",
    "123682.com", "123pan.cc",
)
PAN_API = "https://yun.123pan.com/b/api"
PAN_SHARE_REFERER = "https://yun.123pan.com/"
PAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# signPath(): digit -> letter table from the 123pan web client.
_PAN_TABLE = "adefghlmyijnopkqrstubcvwsz"
PAN_API_INTERVAL = 0.7

# acw_sc__v2 (edge anti-bot cookie) constants from AcwScV2Generator.java.
_ACW_POSITION = (
    15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13, 6,
    11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34, 37, 12, 36,
)
_ACW_MASK = "3000176000856006061501533003690027800375"

RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.6


class LanzouError(RuntimeError):
    """The share could not be resolved or downloaded."""


@dataclass(frozen=True)
class ShareFile:
    name: str
    size: int              # bytes
    direct_url: str
    file_id: str
    user_id: str
    password: Optional[str] = None
    # Extra request headers the CDN requires on every connection (QQ 文件中转
    # rejects ranges without its mail5k cookie).
    headers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- crypto


def encode_hex(text: str, key: bytes = AES_KEY) -> str:
    """AES-128/ECB/PKCS7 -> lowercase hex, the encoding the API expects."""

    raw = text.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()).hex()


def _acw_sc_v2(arg1: str) -> str:
    slots = [""] * len(_ACW_POSITION)
    for index, char in enumerate(arg1[: len(_ACW_POSITION)]):
        for slot, position in enumerate(_ACW_POSITION):
            if position == index + 1:
                slots[slot] = char
    reordered = "".join(slots)
    out = []
    for index in range(0, min(len(reordered), len(_ACW_MASK)), 2):
        out.append(
            f"{int(reordered[index:index + 2], 16) ^ int(_ACW_MASK[index:index + 2], 16):02x}"
        )
    return "".join(out)


def _new_uuid() -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(alphabet) for _ in range(21))


# --------------------------------------------------------------------------- http


def _connection(host: str, timeout: float) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(
        host, 443, context=ssl.create_default_context(), timeout=timeout,
    )


def _headers(host: str, cookie: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": REFERER.rstrip("/"),
        "Referer": REFERER,
        "Host": host,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _request(
    method: str, host: str, path: str, *, cookie: str = "",
    timeout: float = 30.0, follow: bool = False,
) -> tuple[int, bytes, Optional[str]]:
    connection = _connection(host, timeout)
    try:
        connection.request(method, path, body=b"" if method == "POST" else None,
                           headers=_headers(host, cookie))
        response = connection.getresponse()
        body = response.read()
        location = response.getheader("Location")
        if follow and location:
            body = b""
        return response.status, body, location
    finally:
        connection.close()


def _api_get(path: str, *, cookie: str, timeout: float) -> tuple[int, bytes, Optional[str]]:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return _request("GET", API_HOST, path, cookie=cookie, timeout=timeout)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise LanzouError(f"蓝奏 API 请求失败: {exc}") from exc
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise LanzouError("蓝奏 API 请求失败")


def _api_post(path: str, *, cookie: str, timeout: float) -> tuple[int, bytes]:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            status, body, _location = _request(
                "POST", API_HOST, path, cookie=cookie, timeout=timeout,
            )
            return status, body
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise LanzouError(f"蓝奏 API 请求失败: {exc}") from exc
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise LanzouError("蓝奏 API 请求失败")


# --------------------------------------------------------------------------- share


def _browser_headers(host: str, referer: str, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer or f"https://{host}/",
        "Host": host,
    }
    if extra:
        headers.update(extra)
    return headers


def _remember_cookies(set_cookie: str, jar: dict[str, str]) -> None:
    for chunk in (set_cookie or "").split(","):
        pair = chunk.strip().split(";")[0]
        if "=" in pair:
            name, value = pair.split("=", 1)
            if name.strip():
                jar[name.strip()] = value.strip()


def _jar_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in jar.items())


def _http(
    host: str, path: str, *, method: str = "GET", body: Optional[bytes] = None,
    cookie: str = "", referer: str = "",
    extra: Optional[dict[str, str]] = None, timeout: float = 30.0,
    read_limit: int = 0,
) -> tuple[int, bytes, dict[str, str]]:
    """One request to a share/CDN host, gzip decoded.

    Every lanzou host this module talks to gzips its HTML even when the request
    did not ask for it; without this the 2 KB anti-bot page looked like binary
    junk and the parser reported "no sign found".
    """

    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(host, 443, context=context, timeout=timeout)
    try:
        headers = _browser_headers(host, referer, extra)
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(read_limit) if read_limit else response.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        info = {
            "location": response.getheader("Location") or "",
            "set_cookie": response.getheader("Set-Cookie") or "",
            "content_range": response.getheader("Content-Range") or "",
            "content_length": response.getheader("Content-Length") or "",
            "content_type": response.getheader("Content-Type") or "",
            "content_disposition": response.getheader("Content-Disposition") or "",
        }
        return response.status, raw, info
    finally:
        connection.close()


def _solve_challenge(raw: bytes, jar: dict[str, str], host: str) -> bool:
    """Solve acw_sc__v2 into ``jar`` when ``raw`` is the challenge page."""

    if b"var arg1=" not in raw:
        return False
    text = raw.decode("utf-8", "replace")
    arg1 = text.split("var arg1='", 1)[1].split("'", 1)[0]
    jar["acw_sc__v2"] = _acw_sc_v2(arg1)
    LOG.info("lanzou: solved acw_sc__v2 for %s", host)
    return True


def _page(host: str, path: str, *, jar: dict[str, str], referer: str = "") -> tuple[int, str]:
    """GET a page, solving the anti-bot challenge once if it appears."""

    status, raw, info = _http(host, path, cookie=_jar_header(jar), referer=referer)
    _remember_cookies(info["set_cookie"], jar)
    if _solve_challenge(raw, jar, host):
        status, raw, info = _http(host, path, cookie=_jar_header(jar), referer=referer)
        _remember_cookies(info["set_cookie"], jar)
    return status, raw.decode("utf-8", "replace")


def _js_value(page: str, name: str) -> Optional[str]:
    for pattern in (rf"var\s+{name}\s*=\s*'([^']*)'", rf'var\s+{name}\s*=\s*"([^"]*)"'):
        match = re.search(pattern, page)
        if match:
            return match.group(1)
    return None


def _range_total(content_range: str) -> int:
    tail = (content_range or "").rsplit("/", 1)
    if len(tail) == 2 and tail[1].strip().isdigit():
        return int(tail[1].strip())
    return 0


def _follow_cdn(url: str, *, jar: dict[str, str], referer: str, hops: int = 6) -> tuple[str, int]:
    """Walk the challenge/redirect chain to the CDN URL and its real size."""

    for _hop in range(hops):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise LanzouError(f"蓝奏直链无效: {url[:120]}")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        status, raw, info = _http(
            parts.hostname, path, cookie=_jar_header(jar), referer=referer,
            extra={"Range": "bytes=0-0", "Accept": "*/*"}, read_limit=65536,
        )
        _remember_cookies(info["set_cookie"], jar)
        if _solve_challenge(raw, jar, parts.hostname or ""):
            continue
        location = info["location"]
        if status in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            continue
        total = _range_total(info["content_range"])
        if not total and status == 200 and info["content_length"].isdigit():
            if "html" not in info["content_type"]:
                total = int(info["content_length"])
        if status in (200, 206) and total:
            return url, total
        raise LanzouError(f"蓝奏直链返回 HTTP {status}")
    raise LanzouError("蓝奏直链跳转次数过多")


def _pan_sign(api_path: str, os_name: str = "web", version: str = "3") -> tuple[str, str]:
    """The signed query parameter every 123pan API call needs."""

    stamp = str(int(time.time()))
    random_part = str(round(1e7 * random.random()))
    now = datetime.now(timezone(timedelta(hours=8)))
    mapped = "".join(_PAN_TABLE[int(char)] for char in now.strftime("%Y%m%d%H%M"))
    time_sign = str(zlib.crc32(mapped.encode("ascii")))
    payload = "|".join([stamp, random_part, api_path, os_name, version, time_sign])
    return time_sign, f"{stamp}-{random_part}-{zlib.crc32(payload.encode('ascii'))}"


def _pan_call(
    subpath: str, *, query: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None, timeout: float = 30.0,
) -> dict[str, Any]:
    """One signed 123pan API call; returns ``data`` from the envelope."""

    api_path = "/b/api" + subpath
    url = PAN_API + subpath
    sign_key, sign_value = _pan_sign(api_path)
    pairs = [(str(name), str(value)) for name, value in (query or {}).items()]
    pairs.append((sign_key, sign_value))
    target = api_path + "?" + urlencode(pairs, quote_via=quote)
    payload = json.dumps(body).encode("utf-8") if body is not None else None

    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(
        "yun.123pan.com", 443, context=context, timeout=timeout
    )
    try:
        headers = {
            "User-Agent": PAN_UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": PAN_SHARE_REFERER.rstrip("/"),
            "Referer": PAN_SHARE_REFERER,
            "platform": "web",
            "app-version": "3",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        connection.request("POST" if payload is not None else "GET", target,
                           body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    finally:
        connection.close()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        envelope = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise LanzouError(f"123云盘接口返回非 JSON (HTTP {status})") from exc
    if int(envelope.get("code", -1)) != 0:
        raise LanzouError(
            f"123云盘: {envelope.get('message') or envelope.get('code')}"
        )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def _pan_files(share_key: str, share_pwd: str) -> list[dict[str, Any]]:
    """Every file in the share, folders walked (the API pages by folder)."""

    found: list[dict[str, Any]] = []
    pending = ["0"]
    while pending and len(found) < 500:
        parent = pending.pop(0)
        page = 1
        while True:
            data = _pan_call("/share/get", query={
                "limit": 100, "next": "0", "orderBy": "file_id",
                "orderDirection": "desc", "parentFileId": parent,
                "Page": page, "shareKey": share_key, "SharePwd": share_pwd,
            })
            entries = data.get("InfoList") or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                is_folder = str(entry.get("Type")) == "1" or int(entry.get("Size") or 0) == 0
                if is_folder and int(entry.get("Size") or 0) == 0:
                    pending.append(str(entry.get("FileId")))
                else:
                    found.append(entry)
            page += 1
            time.sleep(PAN_API_INTERVAL)
            if not entries or str(data.get("Next")) == "-1":
                break
    return found


def _pan_final(url: str, timeout: float = 30.0) -> tuple[str, int, str]:
    """Resolve the 123pan download URL to the CDN URL and its real size."""

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise LanzouError(f"123云盘下载地址无效: {url[:120]}")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    status, raw, info = _http(
        parts.hostname, path, referer=PAN_SHARE_REFERER,
        extra={"Accept": "*/*", "Range": "bytes=0-0"}, read_limit=65536,
        timeout=timeout,
    )
    final = url
    if status in (301, 302, 303, 307, 308) and info["location"]:
        final = urljoin(url, info["location"])
    elif 200 <= status < 300 and b"redirect_url" in raw:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            payload = {}
        candidate = str(((payload.get("data") or {}) if isinstance(payload, dict) else {})
                         .get("redirect_url") or "")
        if candidate:
            final = candidate
    final_parts = urlsplit(final)
    total = 0
    if final_parts.hostname:
        status, _raw, info = _http(
            final_parts.hostname,
            (final_parts.path or "/")
            + (f"?{final_parts.query}" if final_parts.query else ""),
            referer=PAN_SHARE_REFERER,
            extra={"Accept": "*/*", "Range": "bytes=0-0"}, read_limit=4096,
            timeout=timeout,
        )
        total = _range_total(info["content_range"])
        if not total and status == 200 and info["content_length"].isdigit():
            total = int(info["content_length"])
    return final, total, status


def _resolve_pan(share_key: str, code: Optional[str]) -> ShareFile:
    """123云盘 share -> direct CDN URL of the release archive."""

    share_pwd = code or ""
    files = _pan_files(share_key, share_pwd)
    if not files:
        raise LanzouError("123云盘分享里没有文件")
    zips = [item for item in files if str(item.get("FileName", "")).lower().endswith(".zip")]
    entry = max(zips or files, key=lambda item: int(item.get("Size") or 0))
    name = str(entry.get("FileName") or f"{share_key}.zip")
    size = int(entry.get("Size") or 0)

    data = _pan_call("/share/download/info", body={
        "shareKey": share_key,
        "SharePwd": share_pwd,
        "etag": entry.get("Etag") or "",
        "fileId": int(entry.get("FileId") or 0),
        "s3keyFlag": entry.get("S3KeyFlag") or "",
        "size": size,
    })
    download_url = str(data.get("DownloadURL") or "")
    if not download_url:
        raise LanzouError("123云盘没有返回下载地址")
    # The web client sometimes hides the real URL behind params=<base64>.
    params = parse_qs(urlsplit(download_url).query).get("params") or []
    if params:
        try:
            decoded = base64.b64decode(params[0]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            decoded = ""
        if decoded.startswith("http"):
            download_url = decoded

    final_url, total, _status = _pan_final(download_url)
    LOG.info(
        "123云盘: share %s -> %s (%.1f MB)",
        share_key, urlsplit(final_url).hostname, (total or size) / 1024 / 1024,
    )
    return ShareFile(
        name=name, size=total or size, direct_url=final_url,
        file_id=str(entry.get("FileId") or ""), user_id="", password=code,
    )


def _resolve_direct(url: str) -> ShareFile:
    """A plain HTTPS URL that serves one archive (no share API involved)."""

    name = ""
    total = 0
    final = url
    for _hop in range(6):
        parts = urlsplit(final)
        if parts.scheme != "https" or not parts.hostname:
            raise LanzouError(f"直链无效: {url[:120]}")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        status, _raw, info = _http(
            parts.hostname, path, referer=f"https://{parts.hostname}/",
            extra={"Accept": "*/*", "Range": "bytes=0-0"}, read_limit=65536,
        )
        disposition = info.get("content_disposition") or ""
        if "filename=" in disposition:
            name = disposition.rsplit("filename=", 1)[1].strip().strip('"').strip("'")
        location = info["location"]
        if status in (301, 302, 303, 307, 308) and location:
            final = urljoin(final, location)
            continue
        total = _range_total(info["content_range"])
        if not total and status in (200, 206) and info["content_length"].isdigit():
            total = int(info["content_length"])
        if total:
            break
        raise LanzouError(f"直链返回 HTTP {status}")
    else:
        raise LanzouError("直链跳转次数过多")

    if not name:
        name = Path(urlsplit(final).path).name or "package.zip"
    LOG.info("direct: %s -> %s (%.1f MB)", urlsplit(url).hostname,
             urlsplit(final).hostname, total / 1024 / 1024)
    return ShareFile(
        name=name, size=total, direct_url=final, file_id="", user_id="",
        # Neutral headers: this host wants no lanzou Origin/Referer.
        headers={"Referer": f"https://{urlsplit(final).hostname}/", "Accept": "*/*"},
    )


def _resolve_qq(key: str, share_url: str, code: Optional[str]) -> ShareFile:
    """QQ 邮箱文件中转 share -> CDN URL plus the cookie its ranges need."""

    status, raw, info = _http(
        "wx.mail.qq.com", "/s", method="POST",
        body=f"f=json&k={key}".encode("utf-8"),
        referer=share_url,
        extra={"Accept": "application/json, text/plain, */*"},
    )
    try:
        envelope = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise LanzouError(f"QQ 中转站接口返回非 JSON (HTTP {status})") from exc
    head = envelope.get("head") or {}
    if int(head.get("ret", -1)) != 0:
        raise LanzouError(f"QQ 中转站: {head.get('msg') or head.get('ret')}")
    body = envelope.get("body") or {}
    direct = str(body.get("url") or "").replace("\\x26", "&")
    if not direct:
        raise LanzouError("QQ 中转站没有返回下载地址")
    name = str(body.get("name") or f"{key}.zip")
    size = int(body.get("size") or 0)

    # The share URL itself only redirects; the CDN link comes with a cookie
    # that every range request has to repeat.
    parts = urlsplit(direct)
    status, _raw, info = _http(
        parts.hostname or "", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
        referer=share_url,
    )
    cookie = ""
    for chunk in (info["set_cookie"] or "").split(","):
        pair = chunk.strip().split(";")[0]
        if pair.startswith("mail5k="):
            cookie = pair
            break
    if status in (301, 302, 303, 307, 308) and info["location"]:
        cdn = urljoin(direct, info["location"])
    elif status in (200, 206):
        cdn = direct
    else:
        raise LanzouError(f"QQ 中转站下载页返回 HTTP {status}")

    headers = {"Referer": share_url, "Accept": "*/*"}
    if cookie:
        headers["Cookie"] = cookie
    else:
        LOG.warning("qq: no mail5k cookie in the redirect; ranges may be refused")

    # Probe the CDN for the authoritative size (and prove ranges work).
    target = urlsplit(cdn)
    _status, _raw, probe = _http(
        target.hostname or "",
        (target.path or "/") + (f"?{target.query}" if target.query else ""),
        referer=share_url, extra={**headers, "Range": "bytes=0-0"}, read_limit=4096,
    )
    total = _range_total(probe["content_range"])
    if not total and probe["content_length"].isdigit():
        total = int(probe["content_length"])
    LOG.info(
        "qq: 文件中转 %s -> %s (%.1f MB, cookie %s)",
        key, target.hostname, (total or size) / 1024 / 1024,
        "yes" if cookie else "MISSING",
    )
    return ShareFile(
        name=name, size=total or size, direct_url=cdn, file_id=key,
        user_id="", password=code, headers=headers,
    )


def _resolve_classic(host: str, share_id: str, code: Optional[str]) -> ShareFile:
    """Classic share (lanzoup.com/<id>) -> direct CDN URL."""

    jar: dict[str, str] = {}
    status, page = _page(host, f"/{share_id}", jar=jar)
    if status != 200:
        raise LanzouError(f"蓝奏分享页返回 HTTP {status}")
    if 'id="pwd"' in page or 'name="pwd"' in page:
        raise LanzouError("该蓝奏分享带提取码，请改用无提取码的分享链接")

    title = re.search(r"<title>(.*?)</title>", page)
    name = (title.group(1).strip() if title else "") or f"{share_id}.bin"
    file_id_match = re.search(r"/q/jb/\?f=(\d+)", page)
    file_id = file_id_match.group(1) if file_id_match else ""
    size_match = re.search(r"文件大小：([0-9.]+)\s*([KMG])", page)
    size_hint = 0
    if size_match:
        scale = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[size_match.group(2)]
        size_hint = int(float(size_match.group(1)) * scale)

    iframe = re.search(r'<iframe[^>]+src="([^"]+)"', page)
    if not iframe:
        raise LanzouError("蓝奏分享页没有下载入口（可能是文件夹分享）")
    frame_path = iframe.group(1).replace("&amp;", "&")

    referer = f"https://{host}/{share_id}"
    status, frame = _page(host, frame_path, jar=jar, referer=referer)
    if status != 200:
        raise LanzouError(f"蓝奏下载页返回 HTTP {status}")
    wp_sign = _js_value(frame, "wp_sign")
    ajaxdata = _js_value(frame, "ajaxdata") or ""
    kdns = _js_value(frame, "kdns") or "1"
    domain = re.search(r"var\s+domain1\s*=\s*'([^']+)'", frame)
    api = domain.group(1) if domain else ""
    if not api and file_id:
        api = f"https://apifile.woozooo.com/ajaxfile.php?file={file_id}"
    if not wp_sign or not api:
        raise LanzouError("蓝奏下载页结构已变化，无法解析 sign")

    target = urlsplit(api)
    form = {
        "action": "downprocess",
        "websignkey": ajaxdata,
        "signs": ajaxdata,
        "sign": wp_sign,
        "websign": "",
        "kd": kdns,
        "ves": "1",
    }
    status, raw, _info = _http(
        target.hostname or "", (target.path or "/") + (f"?{target.query}" if target.query else ""),
        method="POST", body=urlencode(form).encode("utf-8"),
        cookie=_jar_header(jar), referer=referer,
    )
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise LanzouError(f"蓝奏直链接口返回非 JSON (HTTP {status})") from exc
    if str(payload.get("zt")) != "1":
        raise LanzouError(f"蓝奏直链获取失败: {payload.get('inf') or payload}")
    direct = str(payload.get("dom") or "").rstrip("/") + "/file/" + str(payload.get("url") or "")
    if code:
        LOG.info("lanzou: classic share declares a password; it is not used by this flow")

    final_url, total = _follow_cdn(direct, jar=jar, referer=referer)
    LOG.info(
        "lanzou: classic share %s -> %s (%.1f MB)",
        share_id, urlsplit(final_url).hostname, total / 1024 / 1024,
    )
    return ShareFile(
        name=name, size=total or size_hint, direct_url=final_url,
        file_id=file_id, user_id="", password=code,
    )


def parse_share_url(url: str) -> tuple[str, str, str, Optional[str]]:
    """Share link -> ``(kind, host, key, code)`` with kind "youxiang" or "classic"."""

    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if not host:
        raise LanzouError(f"不是蓝奏分享链接: {url}")
    segments = [segment for segment in parts.path.split("/") if segment]
    if any(host == known or host.endswith("." + known) for known in SHARE_HOSTS):
        if len(segments) < 2 or segments[0] != "s":
            raise LanzouError(f"链接中没有分享标识: {url}")
        code = (parse_qs(parts.query).get("code") or [None])[0]
        return "youxiang", host, segments[1], (code.strip() or None) if code else None
    if any(host == known or host.endswith("." + known) for known in CLASSIC_HOSTS):
        if not segments:
            raise LanzouError(f"链接中没有分享标识: {url}")
        code = (parse_qs(parts.query).get("pwd") or [None])[0]
        return "classic", host, segments[0], (code.strip() or None) if code else None
    if any(host == known or host.endswith("." + known) for known in QQ_HOSTS):
        query = parse_qs(parts.query)
        key = (query.get("k") or query.get("key") or [""])[0].strip()
        if not key:
            raise LanzouError(f"QQ 中转站链接缺少 k 参数: {url}")
        code = (query.get("code") or [None])[0]
        return "qq", host, key, (code.strip() or None) if code else None
    if any(host == known or host.endswith("." + known) for known in PAN_HOSTS):
        # https://www.123pan.com/s/<shareKey> (the "s" segment is optional in
        # some of the mirror hosts)
        key = segments[1] if len(segments) > 1 and segments[0] == "s" else (
            segments[0] if segments else ""
        )
        if not key:
            raise LanzouError(f"链接中没有分享标识: {url}")
        query = parse_qs(parts.query)
        code = (query.get("pwd") or query.get("code") or [None])[0]
        return "pan", host, key, (code.strip() or None) if code else None
    # Any other well-formed HTTPS URL is a direct archive URL (GitHub release
    # asset, object storage, the operator's own server).  This is the stable
    # option: the same URL keeps working across releases when it is overwritten.
    if parts.scheme == "https":
        return "direct", host, url.strip(), None
    raise LanzouError(f"不支持的链接: {url}")


def resolve(
    share_url: str, *, timeout: float = 30.0, password: Optional[str] = None,
) -> ShareFile:
    """Share link -> the direct URL of the (single) file it holds."""

    kind, host, share_key, url_code = parse_share_url(share_url)
    code = password if password is not None else url_code
    if kind == "classic":
        return _resolve_classic(host, share_key, code)
    if kind == "pan":
        return _resolve_pan(share_key, code)
    if kind == "qq":
        return _resolve_qq(share_key, share_url, code)
    if kind == "direct":
        return _resolve_direct(share_key)
    uuid = _new_uuid()
    ts = encode_hex(str(int(time.time() * 1000)))

    # Warm-up: the site fires this before the list request; failures are harmless.
    try:
        _api_post(
            f"/unproved/buy/vip/list?devType=6&devModel=Chrome&uuid={uuid}"
            f"&extra=2&timestamp={ts}",
            cookie="", timeout=timeout,
        )
    except LanzouError as exc:
        LOG.debug("lanzou warm-up skipped: %s", exc)

    query = (
        f"/unproved/recommend/list?devType=6&devModel=Chrome&uuid={uuid}"
        f"&extra=2&timestamp={ts}&shareId={share_key}&type=0&offset=1&limit=60"
    )
    if code:
        query += f"&code={code}"
    cookie = ""
    status, body = _api_post(query, cookie=cookie, timeout=timeout)
    text = body.decode("utf-8", "replace")
    if "var arg1='" in text:
        # Edge anti-bot: solve acw_sc__v2 once and repeat the request.
        arg1 = text.split("var arg1='", 1)[1].split("'", 1)[0]
        cookie = f"acw_sc__v2={_acw_sc_v2(arg1)}"
        LOG.info("lanzou: anti-bot page detected; retrying with acw_sc__v2")
        status, body = _api_post(query, cookie=cookie, timeout=timeout)
        text = body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LanzouError(f"蓝奏列表返回非 JSON (HTTP {status})") from exc
    if payload.get("code") != 200:
        raise LanzouError(
            f"蓝奏拒绝分享: {payload.get('msg') or payload.get('code')}"
            + ("（可能需要提取码）" if code is None else "")
        )
    items = payload.get("list") or []
    if not items:
        raise LanzouError("蓝奏分享为空或链接已失效")
    item = items[0]
    files = item.get("fileList") or []
    if not files:
        raise LanzouError("该分享是文件夹，安装器只接受单个文件分享")
    entry = files[0]
    file_id = str(entry.get("fileId") or "")
    user_id = str(item.get("userId") or "")
    if not file_id or not user_id:
        raise LanzouError("蓝奏列表缺少 fileId/userId")

    ts2 = encode_hex(str(int(time.time() * 1000)))
    download_id = encode_hex(f"{file_id}|{user_id}")
    auth = encode_hex(f"{file_id}|{ts2}")
    redirect_path = (
        f"/unproved/file/redirect?downloadId={download_id}&enable=1&devType=6"
        f"&uuid={uuid}&timestamp={ts2}&auth={auth}"
    )
    status, body, location = _api_get(redirect_path, cookie=cookie, timeout=timeout)
    if status not in (301, 302, 303, 307, 308) or not location:
        detail = body.decode("utf-8", "replace")[:160]
        raise LanzouError(f"蓝奏直链获取失败 (HTTP {status}): {detail}")

    size_kb = entry.get("fileSize")
    try:
        size = int(float(size_kb)) * 1024
    except (TypeError, ValueError):
        size = 0
    return ShareFile(
        name=str(entry.get("fileName") or f"{share_key}.bin"),
        size=size,
        direct_url=location,
        file_id=file_id,
        user_id=user_id,
        password=code,
    )


# --------------------------------------------------------------------------- download


# Download tuning.  The share CDN limits each connection, not the account, so
# parallel ranges are what make it fast; escalation finds the machine's ceiling.
READ_SIZE = 1024 * 1024
PARTS_BASE = 8
PARTS_MAX = 24
MIN_RANGE = 1024 * 1024
SLOW_RATE_BPS = 8 * 1024 * 1024
WAVE_MIN_BYTES = 8 * 1024 * 1024
# Below this a mirror is unusable *if* the caller has another one to try.
UNUSABLE_RATE_BPS = 1.5 * 1024 * 1024
# How long the watchdog waits before judging a running download (seconds and
# bytes), so a slow start does not abort a source that is merely warming up.
MIN_RATE_SAMPLE_SECONDS = 5.0
MIN_RATE_SAMPLE_BYTES = 256 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_headers(
    host: str, extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Headers for a byte-range request.

    A source that supplied ``headers`` (QQ 中转站: Referer + mail5k cookie) uses
    exactly those plus a neutral Accept; the lanzou defaults only apply when the
    source asked for nothing.
    """

    if extra:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        headers.update(extra)
        return headers
    headers = _headers(host)
    headers["Accept"] = "*/*"
    return headers


def _range_into(
    url: str, start: int, end: int, target: Path,
    progress: Optional[Callable[[int], None]], failures: list,
    stop: Optional[threading.Event] = None,
    headers: Optional[dict[str, str]] = None,
) -> None:
    """Write ``bytes=start-end`` of ``url`` into ``target`` at that offset."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            connection = _connection(host, 30.0)
            try:
                connection.request("GET", path, headers={
                    **_download_headers(host, headers), "Range": f"bytes={start}-{end}",
                })
                response = connection.getresponse()
                if response.status not in (200, 206):
                    raise LanzouError(f"CDN 返回 HTTP {response.status}")
                remaining = end - start + 1
                # One handle per range, seeked to its own offset: the parts never
                # see each other, so no lock is needed and no temp file is copied.
                with target.open("r+b") as handle:
                    handle.seek(start)
                    while remaining > 0:
                        if stop is not None and stop.is_set():
                            raise LanzouError("该来源速度过低，已放弃")
                        chunk = response.read(min(READ_SIZE, remaining))
                        if not chunk:
                            break
                        handle.write(chunk)
                        remaining -= len(chunk)
                        if progress is not None:
                            progress(len(chunk))
            finally:
                connection.close()
            if remaining == 0:
                return
            raise LanzouError("分段下载不完整")
        except Exception as exc:  # noqa: BLE001 - retried below
            if stop is not None and stop.is_set():
                failures.append(f"{start}-{end}: aborted")
                return
            if attempt == RETRY_ATTEMPTS:
                failures.append(f"{start}-{end}: {exc}")
                LOG.warning("lanzou: range %d-%d failed: %s", start, end, exc)
                return
            time.sleep(RETRY_DELAY_SECONDS * attempt)


def _parallel_ranges(
    url: str, target: Path, total: int, *,
    parts: Optional[int], progress: Optional[Callable[[int], None]],
    min_rate_bps: int = 0, headers: Optional[dict[str, str]] = None,
) -> None:
    """Fill ``target`` from ``url`` in slices, escalating when a slice is slow."""

    with target.open("wb") as handle:
        handle.truncate(total)
    streams = PARTS_BASE if parts is None else max(1, min(int(parts), PARTS_MAX))
    slices = 1
    if streams > 1 and total >= 3 * WAVE_MIN_BYTES:
        slices = 3
    elif streams > 1 and total >= WAVE_MIN_BYTES:
        slices = 2
    span = (total + slices - 1) // slices
    for index in range(slices):
        slice_start = index * span
        slice_end = min(total - 1, slice_start + span - 1)
        if slice_start > slice_end:
            break
        length = slice_end - slice_start + 1
        count = max(1, min(streams, max(1, length // MIN_RANGE)))
        chunk = (length + count - 1) // count
        failures: list = []
        threads = []
        started = time.monotonic()
        stop = threading.Event()
        watch = {"bytes": 0}
        watch_lock = threading.Lock()

        def counted(chunk_bytes: int) -> None:
            with watch_lock:
                watch["bytes"] += max(0, int(chunk_bytes))
            if progress is not None:
                progress(chunk_bytes)

        def watchdog() -> None:
            while not stop.wait(0.5):
                elapsed = time.monotonic() - started
                if elapsed < MIN_RATE_SAMPLE_SECONDS:
                    continue
                with watch_lock:
                    current = watch["bytes"]
                if current < MIN_RATE_SAMPLE_BYTES:
                    continue
                rate = current / elapsed
                if rate < min_rate_bps:
                    LOG.warning(
                        "lanzou: %.1f MB/s after %.0fs, abandoning this source",
                        rate / 1024 / 1024, elapsed,
                    )
                    stop.set()
                    return

        watcher = None
        if min_rate_bps > 0:
            watcher = threading.Thread(target=watchdog, daemon=True)
            watcher.start()
        for slot in range(count):
            range_start = slice_start + slot * chunk
            if range_start > slice_end:
                break
            range_end = min(slice_end, range_start + chunk - 1)
            thread = threading.Thread(
                target=_range_into,
                args=(url, range_start, range_end, target, counted, failures, stop, headers),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        stop.set()
        if watcher is not None:
            watcher.join(timeout=1.0)
        if (min_rate_bps and watch["bytes"] < total
                and any("aborted" in failure for failure in failures)):
            raise LanzouError("该来源速度过低，改用其它来源")
        if failures:
            raise LanzouError(f"蓝奏分段下载失败：{failures[0]}")
        elapsed = max(1e-6, time.monotonic() - started)
        rate = length / elapsed
        LOG.info(
            "lanzou: slice %d/%d %.1f MB in %.1fs (%.1f MB/s, %d stream(s))",
            index + 1, slices, length / 1024 / 1024, elapsed,
            rate / 1024 / 1024, count,
        )
        if index + 1 < slices and rate < SLOW_RATE_BPS and streams < PARTS_MAX:
            # This machine's per-connection ceiling is the limit: ask for more.
            streams = min(PARTS_MAX, streams * 2)
            LOG.info("lanzou: slow slice, raising to %d stream(s)", streams)
        if min_rate_bps and rate < min_rate_bps:
            # Only set when the caller has another mirror to try: the route is
            # unusable (10 minutes for 93 MB), so do not spend it here.
            raise LanzouError(
                f"该来源速度过低（{rate / 1024 / 1024:.1f} MB/s），改用其它来源"
            )


def _probe_direct_url(
    url: str, timeout: float = 30.0, headers: Optional[dict[str, str]] = None,
) -> tuple[bool, int]:
    """Return ``(range_supported, total_bytes)`` for the CDN URL.

    Lanzou's ``fileSize`` is expressed in whole KB and is rounded UP, so it can
    overshoot the real payload (93.6 MB share: 95817 KB = 98,116,608 reported vs
    98,116,260 actual).  Splitting on the reported value makes the last range
    cross EOF, which OpenCV's sink reports as "分段下载不完整".  The CDN answers
    ``Range: bytes=0-0`` with the authoritative ``Content-Range`` total.
    """

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = _connection(host, timeout)
    try:
        connection.request("GET", path, headers={
            **_download_headers(host, headers), "Range": "bytes=0-0",
        })
        response = connection.getresponse()
        status = response.status
        content_range = response.getheader("Content-Range") or ""
        length = response.getheader("Content-Length") or ""
        response.read(1024)
        total = 0
        if content_range.startswith("bytes"):
            with_slash = content_range.rsplit("/", 1)
            if len(with_slash) == 2 and with_slash[1].isdigit():
                total = int(with_slash[1])
        elif length.isdigit():
            total = int(length)
        return (status == 206 and total > 0), total
    except (OSError, ssl.SSLError, http.client.HTTPException):
        LOG.debug("lanzou: direct-URL probe failed", exc_info=True)
        return False, 0
    finally:
        connection.close()


def _stream_download(
    url: str, target: Path, *, progress: Optional[Callable[[int], None]] = None,
    timeout: float = 30.0, headers: Optional[dict[str, str]] = None,
) -> int:
    """Single-connection download, used when the CDN ignores Range."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = _connection(host, timeout)
    written = 0
    try:
        connection.request("GET", path, headers=_download_headers(host, headers))
        response = connection.getresponse()
        if response.status >= 400:
            raise LanzouError(f"CDN 返回 HTTP {response.status}")
        with target.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if progress is not None:
                    progress(len(chunk))
    finally:
        connection.close()
    return written


def download(
    share: ShareFile, target: Path, *,
    expected_sha256: Optional[str] = None,
    expected_size: Optional[int] = None,
    parts: Optional[int] = None,
    progress: Optional[Callable[[int], None]] = None,
    min_rate_bps: int = 0,
) -> Path:
    """Download the share into ``target`` (parallel ranges, then verify).

    The direct URL is untrusted transport: pass the SHA-256 from the pinned
    manifest and it is checked before the file is moved into place.
    """

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    range_supported, total = _probe_direct_url(share.direct_url, headers=share.headers)
    if expected_size and total and abs(total - int(expected_size)) > 4096:
        LOG.warning(
            "lanzou: the CDN reports %d bytes while the share listed %d",
            total, int(expected_size),
        )

    started = time.monotonic()
    try:
        if range_supported and total > 0:
            _parallel_ranges(
                share.direct_url, target, total, parts=parts, progress=progress,
                min_rate_bps=min_rate_bps, headers=share.headers,
            )
        else:
            LOG.info("lanzou: the CDN does not support Range; using one stream")
            with tempfile.TemporaryDirectory(
                prefix="lanzou-", dir=str(target.parent)
            ) as tmp:
                assembled = Path(tmp) / "assembled.bin"
                _stream_download(
                    share.direct_url, assembled, progress=progress,
                    headers=share.headers,
                )
                os.replace(assembled, target)

        elapsed = max(1e-6, time.monotonic() - started)
        got = target.stat().st_size
        LOG.info(
            "lanzou: downloaded %.1f MB in %.1fs (%.1f MB/s, %s)",
            got / 1024 / 1024, elapsed, got / 1024 / 1024 / elapsed,
            "parallel ranges" if range_supported else "single stream",
        )
        if total and got != total:
            raise LanzouError(f"蓝奏下载大小不符: {got} != {total}")
        if expected_sha256:
            digest = file_sha256(target)
            if digest.lower() != expected_sha256.strip().lower():
                raise LanzouError(
                    f"蓝奏文件 SHA-256 不符（期望 {expected_sha256[:16]}…，"
                    f"实得 {digest[:16]}…）；已拒绝安装"
                )
    except BaseException:
        # A partial file at the target path must never look like a download.
        target.unlink(missing_ok=True)
        raise
    return target


def main(argv: Optional[list[str]] = None) -> int:
    """CLI self-test: resolve a share, optionally download and verify it."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("share", help="https://www.ilanzou.com/s/<key>[?code=<pwd>]")
    parser.add_argument("--password", default=None, help="提取码 (overrides ?code=)")
    parser.add_argument("--out", default=None, help="download here (default: name)")
    parser.add_argument("--sha256", default=None, help="expected SHA-256 to verify")
    parser.add_argument("--parts", type=int, default=None)
    parser.add_argument("--resolve-only", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    share = resolve(args.share, password=args.password)
    print(f"name      : {share.name}")
    print(f"size      : {share.size} bytes ({share.size / 1024 / 1024:.1f} MB)")
    print(f"fileId    : {share.file_id}   userId: {share.user_id}")
    print(f"direct    : {share.direct_url[:120]}...")
    if args.resolve_only:
        return 0
    target = Path(args.out or share.name)
    download(share, target, expected_sha256=args.sha256,
             expected_size=share.size or None, parts=args.parts)
    print(f"saved     : {target} ({target.stat().st_size} bytes)")
    print(f"sha256    : {file_sha256(target)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
