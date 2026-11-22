"""preview.py 단위 테스트. ffmpeg 는 가짜 runner 로 대신한다."""

from pathlib import Path

import pytest

from capped_video_mcp import preview as P
from capped_video_mcp.config import PreviewConfig

JPEG = b"\xff\xd8\xff\xdb" + b"x" * 32 + b"\xff\xd9"


def test_build_args_tiles_and_fps():
    args = P.build_args("ffmpeg", Path("v.mp4"), 6, PreviewConfig(frames=6, frame_width=320))
    vf = args[args.index("-vf") + 1]
    assert "tile=3x2" in vf and "scale=320:-2" in vf and vf.startswith("fps=1.03")
    assert "-nostdin" in args and args[-1] == "pipe:1" and "-frames:v" in args
    vf4 = P.build_args("ffmpeg", Path("v.mp4"), 4, PreviewConfig(frames=4))[7]
    assert "tile=3x2" in vf4  # 4장도 3열 2행


async def test_contact_sheet_uses_runner_and_cache(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"0" * 100)
    fake = tmp_path / "ffmpeg"
    fake.write_bytes(b"")
    calls = []

    async def runner(args):
        calls.append(args)
        return JPEG
    cfg = PreviewConfig(ffmpeg=str(fake))
    assert await P.contact_sheet(video, 5, cfg, runner=runner) == JPEG
    assert (tmp_path / "a.preview.jpg").read_bytes() == JPEG
    assert await P.contact_sheet(video, 5, cfg, runner=runner) == JPEG and len(calls) == 1
    # 영상이 새로 바뀌면 다시 만든다
    import os
    import time
    os.utime(video, (time.time() + 10, time.time() + 10))
    await P.contact_sheet(video, 5, cfg, runner=runner)
    assert len(calls) == 2


async def test_contact_sheet_rejects_non_jpeg_and_missing_ffmpeg(tmp_path, monkeypatch):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"0")

    async def bad(args):
        return b"not a jpeg"
    fake = tmp_path / "ffmpeg"
    fake.write_bytes(b"")
    with pytest.raises(P.PreviewUnavailable, match="JPEG"):
        await P.contact_sheet(video, 5, PreviewConfig(ffmpeg=str(fake)), runner=bad)
    with pytest.raises(P.PreviewUnavailable, match="ffmpeg"):
        await P.contact_sheet(video, 5, PreviewConfig(ffmpeg=str(tmp_path / "nope")), runner=bad)
    monkeypatch.setattr(P.shutil, "which", lambda _: None)
    with pytest.raises(P.PreviewUnavailable, match="CAPPED_VIDEO_FFMPEG"):
        await P.contact_sheet(video, 5, PreviewConfig(), runner=bad)


async def test_run_ffmpeg_failure_message(tmp_path):
    import sys
    # 실제 하위 프로세스 실행 경로: 파이썬을 ffmpeg 대신 돌려 실패시킨다
    args = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"]
    with pytest.raises(P.PreviewUnavailable, match="boom"):
        await P._run_ffmpeg(args)
    ok = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xd8ok')"]
    assert (await P._run_ffmpeg(ok)).startswith(b"\xff\xd8")
