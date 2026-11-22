"""완료된 영상을 안전하게 내려받는다.

- URL 은 제공처 응답에서 얻은 것만 쓴다 (에이전트 입력을 받지 않는다).
- https + 허용 도메인만, 리다이렉트도 같은 규칙으로 검사한다.
- 인증 헤더를 붙이지 않는다 (다른 도메인으로 키가 새지 않게).
- 크기·형식 검사, output/<job_id>.mp4 고정 경로.
- 시도마다 고유한 .part 임시 파일에 받은 뒤 이름을 바꾼다 (동시 다운로드가 같은 파일에 쓰지 않게).
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

JOB_ID_RE = re.compile(r"j_\d{8}_\d{6}_[0-9a-f]{6}")
MAX_REDIRECTS = 3
HEARTBEAT_SEC = 30.0
# 일부 CDN 은 영상을 일반 바이너리 형식으로 준다. 이때는 MP4 시그니처(ftyp)로 확인한다.
GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
MP4_MAGIC_OFFSET = 4
MP4_MAGIC = b"ftyp"


class DownloadError(Exception):
    """gone=True: 원격 결과가 사라졌다 (404/410). 다시 시도해도 소용없다."""

    def __init__(self, message: str, *, gone: bool = False):
        super().__init__(message)
        self.message = message
        self.gone = gone


def host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in (a.lower() for a in allowed_hosts))


def target_path(dest_dir: Path, job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise DownloadError(f"잘못된 작업 ID: {job_id!r}")
    return dest_dir / f"{job_id}.mp4"


async def download_video(
    client: httpx.AsyncClient,
    url: str,
    dest_dir: Path,
    job_id: str,
    allowed_hosts: list[str],
    max_bytes: int,
    *,
    on_progress: Callable[[], None] | None = None,
) -> Path:
    dest = target_path(dest_dir, job_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    part = dest_dir / f"{job_id}.{secrets.token_hex(4)}.part"

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not host_allowed(current, allowed_hosts):
            raise DownloadError(f"허용되지 않은 다운로드 주소입니다: {urlsplit(current).hostname or current[:50]}")
        try:
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("Location")
                    if not location:
                        raise DownloadError("리다이렉트 주소가 없습니다.")
                    current = urljoin(current, location)
                    continue
                if resp.status_code in (404, 410):
                    raise DownloadError("원격 결과가 이미 삭제됐습니다.", gone=True)
                if resp.status_code >= 400:
                    raise DownloadError(f"다운로드 실패(HTTP {resp.status_code}).")
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                is_video = ctype.startswith("video/")
                if not is_video and ctype not in GENERIC_TYPES:
                    raise DownloadError(f"영상 형식이 아닙니다: {ctype or '없음'}")
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise DownloadError(f"파일이 너무 큽니다 ({int(length)} bytes).")
                try:
                    await _save(resp, part, max_bytes, need_magic=not is_video, on_progress=on_progress)
                    os.replace(part, dest)
                except BaseException:
                    part.unlink(missing_ok=True)
                    raise
                return dest
        except httpx.TransportError as e:
            raise DownloadError("다운로드 중 네트워크 오류.") from e
    raise DownloadError("리다이렉트가 너무 많습니다.")


async def _save(
    resp: httpx.Response,
    part: Path,
    max_bytes: int,
    *,
    need_magic: bool,
    on_progress: Callable[[], None] | None,
) -> None:
    received = 0
    head = b""
    last_beat = time.monotonic()
    with part.open("wb") as f:
        async for chunk in resp.aiter_bytes():
            received += len(chunk)
            if received > max_bytes:
                raise DownloadError(f"파일이 너무 큽니다 (>{max_bytes} bytes).")
            if need_magic and len(head) < MP4_MAGIC_OFFSET + len(MP4_MAGIC):
                head += chunk[: 16]
                if len(head) >= MP4_MAGIC_OFFSET + len(MP4_MAGIC) and \
                        head[MP4_MAGIC_OFFSET:MP4_MAGIC_OFFSET + len(MP4_MAGIC)] != MP4_MAGIC:
                    raise DownloadError("영상 파일(MP4)이 아닙니다.")
            f.write(chunk)
            if on_progress and time.monotonic() - last_beat >= HEARTBEAT_SEC:
                on_progress()
                last_beat = time.monotonic()
    if received == 0:
        raise DownloadError("받은 파일이 비어 있습니다.")
    if need_magic and head[MP4_MAGIC_OFFSET:MP4_MAGIC_OFFSET + len(MP4_MAGIC)] != MP4_MAGIC:
        raise DownloadError("영상 파일(MP4)이 아닙니다.")
