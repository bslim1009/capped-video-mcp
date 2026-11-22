"""완성된 영상의 미리보기(프레임 여러 장을 한 이미지로). ffmpeg 가 있을 때만 동작한다.

에이전트는 영상을 볼 수 없으므로, 프레임 묶음 이미지를 돌려줘서 결과를 보고 재생성 여부를 판단하게 한다.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import PreviewConfig

log = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SEC = 60
Runner = Callable[[list[str]], Awaitable[bytes]]


class PreviewUnavailable(Exception):
    """미리보기를 만들 수 없다. message 는 에이전트에게 보여도 되는 문장."""


def find_ffmpeg(cfg: PreviewConfig) -> str | None:
    if cfg.ffmpeg:
        return cfg.ffmpeg if Path(cfg.ffmpeg).is_file() else None
    return shutil.which("ffmpeg")


def build_args(ffmpeg: str, video: Path, duration_sec: int, cfg: PreviewConfig) -> list[str]:
    """프레임 cfg.frames 장을 가로 3장씩 타일로 붙인 JPEG 한 장을 stdout 으로 낸다."""
    cols = min(3, cfg.frames)
    rows = -(-cfg.frames // cols)
    # fps = 장수/길이 로 고르게 뽑는다. 마지막 프레임 누락을 피하려고 길이보다 살짝 짧게 잡는다.
    fps = cfg.frames / max(0.5, duration_sec - 0.2)
    return [
        ffmpeg, "-v", "error", "-nostdin", "-i", str(video),
        "-vf", f"fps={fps:.4f},scale={cfg.frame_width}:-2,tile={cols}x{rows}",
        "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1",
    ]


async def _run_ffmpeg(args: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        raise PreviewUnavailable(f"ffmpeg 가 {FFMPEG_TIMEOUT_SEC}초 안에 끝나지 않았습니다.") from None
    if proc.returncode != 0 or not out:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        raise PreviewUnavailable(f"ffmpeg 실패: {msg[-1] if msg else f'exit {proc.returncode}'}")
    return out


async def contact_sheet(video: Path, duration_sec: int, cfg: PreviewConfig, *, runner: Runner | None = None) -> bytes:
    """video 옆의 <이름>.preview.jpg 를 돌려준다. 없으면 ffmpeg 로 만들어 저장한다."""
    cache = video.with_suffix(".preview.jpg")
    if cache.is_file() and cache.stat().st_mtime >= video.stat().st_mtime:
        return cache.read_bytes()
    ffmpeg = find_ffmpeg(cfg)
    if ffmpeg is None:
        raise PreviewUnavailable(
            "ffmpeg 를 찾을 수 없습니다. 설치해서 PATH 에 두거나 CAPPED_VIDEO_FFMPEG 로 경로를 지정하세요. "
            "미리보기 없이도 영상 파일은 그대로 쓸 수 있습니다."
        )
    data = await (runner or _run_ffmpeg)(build_args(ffmpeg, video, duration_sec, cfg))
    if not data.startswith(b"\xff\xd8"):
        raise PreviewUnavailable("ffmpeg 출력이 JPEG 가 아닙니다.")
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache)
    except OSError as e:  # 캐시 실패는 치명적이지 않다
        log.warning("미리보기 캐시 저장 실패 %s: %s", cache, e)
    return data
