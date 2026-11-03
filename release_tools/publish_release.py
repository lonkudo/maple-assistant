"""Publish a separately-built MoonEclipse package to the activation server.

The server stages every byte before it publishes anything, so an interrupted or
half-failed transfer can never become the current release:

  1. POST /api/v1/operator/releases/prepare          clear that version's staging
  2. PUT  /api/v1/operator/releases/files/<v>/<path> one authenticated PUT per file
  3. POST /api/v1/operator/releases/publish          verify the staged bytes and
                                                    promote the tree atomically

Step 2 is written by Nginx itself after ``authorize_release_upload()`` approves
the path, which is why the transport is ``pinned_tls.upload_binary()`` rather
than a form post: a single multipart body (the previous design) is rejected by
``publish_release()``, which parses JSON only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from licensing import ACTIVATION_SERVER_ENDPOINT
from pinned_tls import PinnedTlsError, PinnedTlsResponseError, post_json, upload_binary


PIN_PATH = ROOT / "activation_server_pin.json"
MANIFEST_FORMAT = "moon-eclipse-release-manifest/v1"
UPLOAD_PATH = "/api/v1/operator/releases/files"
# Only the compiled package may be published: shipping the Python sources to
# every installer client is what the protected build exists to prevent.
PROTECTED_EXECUTABLE = "MoonEclipse.exe"
SOURCE_MARKER = "assistant.py"
# Matches release_api.VERSION_PATTERN so a bad version fails here instead of
# after a full upload.
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9._-]{0,16})$")
MAX_UPLOAD_WORKERS = 4
TOKEN_ENVIRONMENT = "MOONECLIPSE_RELEASE_PUBLISH_TOKEN"

EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "work", "release", "助手配置", "data", "recordings", "updates", "logs"}
# Local scratch the packaging step leaves beside the staged sources.  These are
# not part of the product, so they must never reach an installer manifest.
EXCLUDED_FILES = {
    "新配置.json",
    "运行日志.txt",
    # The YOLO panel is disabled in this build (_SHOW_YOLO_PANEL = False) and no
    # shipped module references either weight file; both are 17.5 MB of dead
    # payload.
    "yolo-detection/weights/best.pt",
    "yolo-detection/weights/best.onnx",
    # AVIF codec for a project whose images are JPG only.
    "PIL/_avif.pyd",
    # Developer instructions belong to the repository, never to a customer.
    "AGENTS.md",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(512 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_for(source: Path, version: str) -> tuple[dict, list[Path]]:
    files: list[Path] = []
    rows: list[dict] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.relative_to(source).parts):
            continue
        relative = path.relative_to(source).as_posix()
        if relative in EXCLUDED_FILES:
            continue
        rows.append({"path": relative, "sha256": sha256(path), "size": path.stat().st_size, "file_version": version})
        files.append(path)
    if not rows:
        raise ValueError("release folder contains no publishable files")
    return {"format": MANIFEST_FORMAT, "version": version, "files": rows}, files


def _authorized(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def prepare_staging(token: str, manifest: dict) -> dict:
    """Clear the version's staging tree; every later PUT lands inside it."""

    answer = post_json(
        ACTIVATION_SERVER_ENDPOINT, "/api/v1/operator/releases/prepare",
        {"manifest": manifest}, PIN_PATH, headers=_authorized(token),
    )
    if not answer.get("ok"):
        raise RuntimeError(f"prepare rejected: {answer.get('code', 'UNKNOWN')}")
    return answer


def _put_file(token: str, version: str, item: dict, source: Path, attempts: int = 3) -> str:
    # The path is percent-encoded because a release contains Chinese filenames
    # (安装.bat, 启动助手.bat) and http.client writes the request line as ASCII.
    target = f"{UPLOAD_PATH}/{quote(version, safe='')}/" + quote(str(item["path"]), safe="/")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            # A fresh handle per attempt: a failed upload consumes the stream.
            with source.open("rb") as handle:
                upload_binary(ACTIVATION_SERVER_ENDPOINT, target, PIN_PATH, token, handle, int(item["size"]))
            return str(item["path"])
        except PinnedTlsResponseError:
            # An authorization or validation refusal will not improve by retrying.
            raise
        except (PinnedTlsError, OSError) as exc:
            # A release is over a thousand PUTs across the public internet; one
            # reset must not throw away the whole transfer.  PUT is idempotent, so
            # retrying the same path simply overwrites an incomplete file.
            last = exc
            if attempt < attempts:
                time.sleep(1.5 * attempt)
    raise PinnedTlsError(f"{item['path']}: {last!r}")


def publish_release(token: str, manifest: dict) -> dict:
    return post_json(
        ACTIVATION_SERVER_ENDPOINT, "/api/v1/operator/releases/publish",
        {"manifest": manifest}, PIN_PATH, timeout=60.0, headers=_authorized(token),
    )


def _require_protected_payload(source: Path) -> None:
    """Refuse anything that is not the compiled package.

    The manifest validates either way, so nothing else stops a source tree from
    being published: pointing --source at release\\MoonEclipse shipped every
    .py file to installer clients.  Require the executable and reject the
    source markers explicitly.
    """

    if (source / SOURCE_MARKER).is_file():
        raise SystemExit(
            f"--source contains {SOURCE_MARKER}: that is the source package. "
            "Publish the protected build instead, for example "
            "release\\MoonEclipse-Protected\\assistant.dist"
        )
    if not (source / PROTECTED_EXECUTABLE).is_file():
        raise SystemExit(
            f"--source has no {PROTECTED_EXECUTABLE}, so it is not a protected "
            "package (expected release\\MoonEclipse-Protected\\assistant.dist)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish MoonEclipse installer files")
    parser.add_argument("--source", type=Path, required=True, help="protected package directory, for example release\\MoonEclipse-Protected\\assistant.dist")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--jobs", type=int, default=MAX_UPLOAD_WORKERS,
        help=f"concurrent uploads (default {MAX_UPLOAD_WORKERS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="build and print the manifest without contacting the server",
    )
    args = parser.parse_args()

    version = args.version.strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise SystemExit(f"invalid version {version!r}: expected X.Y.Z")
    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit("--source must be a package directory")
    _require_protected_payload(source)

    manifest, files = manifest_for(source, version)
    total = len(manifest["files"])
    if args.dry_run:
        print(json.dumps({"version": version, "files": total, "manifest": manifest}, ensure_ascii=False))
        return 0

    token = os.environ.get(TOKEN_ENVIRONMENT, "").strip()
    if not token:
        raise SystemExit(f"{TOKEN_ENVIRONMENT} is required")

    prepare_staging(token, manifest)
    print(f"staged {total} file(s) for {version}", file=sys.stderr)

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = {
                pool.submit(_put_file, token, version, item, source / item["path"]): item
                for item in manifest["files"]
            }
            for future in as_completed(futures):
                future.result()
                done += 1
                if done % 10 == 0 or done == total:
                    print(f"  uploaded {done}/{total}", file=sys.stderr)
    except PinnedTlsResponseError as exc:
        raise SystemExit(f"upload rejected HTTP {exc.status} ({exc.code}): {exc.message}") from None
    except PinnedTlsError as exc:
        raise SystemExit(f"upload failed after {done}/{total} files: {exc}") from None

    result = publish_release(token, manifest)
    bundle_bytes = int(result.get("bundle_bytes") or 0)
    print(json.dumps({
        "version": result.get("version"),
        "files": result.get("files"),
        "bundle_bytes": bundle_bytes,
    }, ensure_ascii=False))
    if bundle_bytes <= 0:
        # The manifest is published, but every installer will fetch one file at a
        # time until the archive exists.  Say so instead of printing success only.
        print(
            "WARNING: the server reported no bundle (bundle_bytes=0); installers will "
            "fall back to one request per file. See DEPLOY_RELEASE.md section 7.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
