"""실제 서버 프로세스를 stdio 로 띄워 확인한다 (mock 제공처).

stdout 에 로그가 섞이면 프로토콜이 깨지므로 이 테스트가 실패한다.
"""

import os
import sys

from mcp import Client, StdioServerParameters


async def test_stdio_server_end_to_end(tmp_path):
    # 데이터 폴더에 config.toml 이 없어도 패키지 기본 설정으로 떠야 한다 (uvx 설치와 같은 상황).
    # 개발자 셸의 CAPPED_VIDEO_* 는 빼고 시작한다.
    base = {k: v for k, v in os.environ.items() if not k.upper().startswith("CAPPED_VIDEO_")}
    env = {**base, "CAPPED_VIDEO_HOME": str(tmp_path), "CAPPED_VIDEO_PROVIDER": "mock", "PYTHONIOENCODING": "utf-8",
           "CAPPED_VIDEO_DEFAULT_ASPECT_RATIO": "9:16"}  # 등록 때 넘기는 환경 변수가 반영되는지
    env.pop("REPLICATE_API_TOKEN", None)
    env.pop("FAL_KEY", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "capped_video_mcp"], env=env)

    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert "generate_video" in tools
        m = await c.call_tool("list_models", {})
        assert m.structured_content["defaults"]["aspect_ratio"] == "9:16"
        r = await c.call_tool("generate_video", {"prompt": "a red paper boat", "duration_sec": 4})
        assert not r.is_error, r.content
        job_id = r.structured_content["job_id"]
        s = await c.call_tool("get_video_status", {"job_id": job_id, "wait_seconds": 30})
        assert s.structured_content["status"] == "succeeded", s.structured_content

    # mock 모드는 실제 기록·출력과 분리된다
    assert (tmp_path / "output" / "mock" / f"{job_id}.mp4").exists()
    assert (tmp_path / "data" / "usage-mock.json").exists()
    assert not (tmp_path / "data" / "usage.json").exists()
    assert (tmp_path / "logs" / "audit.jsonl").exists()
