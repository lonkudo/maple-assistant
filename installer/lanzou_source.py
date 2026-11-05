"""Resolve a 蓝奏优享 (ilanzou) share link to a direct download URL.

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
import hashlib
import http.client
import json
import logging
import os
import random
import ssl
import string
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

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


def parse_share_url(url: str) -> tuple[str, Optional[str]]:
    """``https://www.ilanzou.com/s/AbCdE?code=xyz`` -> ``("AbCdE", "xyz")``."""

    parts = urlsplit(url.strip())
    if parts.hostname is None or not any(
        parts.hostname.endswith(host) for host in SHARE_HOSTS
    ):
        raise LanzouError(f"不是蓝奏优享分享链接: {url}")
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "s":
        raise LanzouError(f"链接中没有分享标识: {url}")
    share_key = segments[1]
    code = (parse_qs(parts.query).get("code") or [None])[0]
    return share_key, (code.strip() or None) if code else None


def resolve(
    share_url: str, *, timeout: float = 30.0, password: Optional[str] = None,
) -> ShareFile:
    """Share link -> the direct URL of the (single) file it holds."""

    share_key, url_code = parse_share_url(share_url)
    code = password if password is not None else url_code
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _range_part(
    url: str, index: int, start: int, end: int, folder: Path,
    results: list, progress: Optional[Callable[[int], None]],
) -> None:
    host = urlsplit(url).hostname or ""
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    target = folder / f"part{index:03d}"
    written = 0
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            connection = _connection(host, 30.0)
            connection.request("GET", path, headers={
                **_headers(host), "Range": f"bytes={start}-{end}",
            })
            response = connection.getresponse()
            if response.status not in (200, 206):
                raise LanzouError(f"CDN 返回 HTTP {response.status}")
            with target.open("wb") as handle:
                remaining = end - start + 1
                while remaining > 0:
                    chunk = response.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(len(chunk))
            connection.close()
            if remaining == 0:
                results[index] = target
                return
            raise LanzouError("分段下载不完整")
        except Exception as exc:  # noqa: BLE001 - retried below
            if attempt == RETRY_ATTEMPTS:
                results[index] = None
                LOG.warning("lanzou: part %d failed: %s", index, exc)
                return
            time.sleep(RETRY_DELAY_SECONDS * attempt)


def _probe_direct_url(url: str, timeout: float = 30.0) -> tuple[bool, int]:
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
            **_headers(host), "Range": "bytes=0-0",
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
    timeout: float = 30.0,
) -> int:
    """Single-connection download, used when the CDN ignores Range."""

    parts = urlsplit(url)
    host = parts.hostname or ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = _connection(host, timeout)
    written = 0
    try:
        connection.request("GET", path, headers=_headers(host))
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
    parts: int = 8,
    progress: Optional[Callable[[int], None]] = None,
) -> Path:
    """Download the share into ``target`` (parallel ranges, then verify).

    The direct URL is untrusted transport: pass the SHA-256 from the pinned
    manifest and it is checked before the file is moved into place.
    """

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    range_supported, total = _probe_direct_url(share.direct_url)
    if expected_size and total and abs(total - int(expected_size)) > 4096:
        LOG.warning(
            "lanzou: the CDN reports %d bytes while the share listed %d",
            total, int(expected_size),
        )

    with tempfile.TemporaryDirectory(prefix="lanzou-", dir=str(target.parent)) as tmp:
        folder = Path(tmp)
        assembled = folder / "assembled.bin"
        started = time.monotonic()
        if range_supported and total > 0:
            parts = max(1, min(int(parts), 16))
            chunk = (total + parts - 1) // parts
            results: list = [None] * parts
            threads = []
            for index in range(parts):
                start = index * chunk
                end = min(total - 1, start + chunk - 1)
                thread = threading.Thread(
                    target=_range_part,
                    args=(share.direct_url, index, start, end, folder, results, progress),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            if any(result is None for result in results):
                raise LanzouError("蓝奏分段下载失败（某个分段多次重试后仍失败）")
            with assembled.open("wb") as out:
                for index in range(parts):
                    out.write(results[index].read_bytes())
        else:
            LOG.info("lanzou: the CDN does not support Range; using one stream")
            _stream_download(share.direct_url, assembled, progress=progress)

        elapsed = max(1e-6, time.monotonic() - started)
        got = assembled.stat().st_size
        LOG.info(
            "lanzou: downloaded %.1f MB in %.1fs (%.1f MB/s, %s parts)",
            got / 1024 / 1024, elapsed, got / 1024 / 1024 / elapsed,
            parts if range_supported else "single",
        )
        if total and got != total:
            raise LanzouError(f"蓝奏下载大小不符: {got} != {total}")
        if expected_sha256:
            digest = file_sha256(assembled)
            if digest.lower() != expected_sha256.strip().lower():
                raise LanzouError(
                    f"蓝奏文件 SHA-256 不符（期望 {expected_sha256[:16]}…，"
                    f"实得 {digest[:16]}…）；已拒绝安装"
                )
        os.replace(assembled, target)
    return target


def main(argv: Optional[list[str]] = None) -> int:
    """CLI self-test: resolve a share, optionally download and verify it."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("share", help="https://www.ilanzou.com/s/<key>[?code=<pwd>]")
    parser.add_argument("--password", default=None, help="提取码 (overrides ?code=)")
    parser.add_argument("--out", default=None, help="download here (default: name)")
    parser.add_argument("--sha256", default=None, help="expected SHA-256 to verify")
    parser.add_argument("--parts", type=int, default=8)
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
