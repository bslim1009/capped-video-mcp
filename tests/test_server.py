"""MCP 프로토콜 수준 테스트: 메모리 안에서 클라이언트로 서버에 연결한다."""

from mcp import Client

from capped_video_mcp.server import create_server


async def test_tools_listed(make_app):
    server = create_server(make_app())
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"generate_video", "get_video_status", "download_video", "get_usage", "list_models",
                          "estimate_video", "preview_video"}
    assert tools["preview_video"].annotations.read_only_hint is True
    assert "ctx" not in tools["generate_video"].input_schema["properties"]
    assert tools["estimate_video"].annotations.read_only_hint is True
    assert "과금" in tools["generate_video"].description
    assert tools["get_usage"].annotations.read_only_hint is True
    props = tools["generate_video"].input_schema["properties"]
    assert {"request_id", "force"} <= set(props)
    assert "duplicate" in tools["generate_video"].description


async def test_generate_and_status_via_protocol(make_app):
    server = create_server(make_app())
    async with Client(server) as c:
        r = await c.call_tool("generate_video", {"prompt": "a red paper boat", "duration_sec": 4})
        assert not r.is_error
        job_id = r.structured_content["job_id"]
        s = await c.call_tool("get_video_status", {"job_id": job_id, "wait_seconds": 60})
        assert not s.is_error
        assert s.structured_content["status"] == "succeeded"


async def test_policy_error_is_tool_error(make_app):
    server = create_server(make_app())
    async with Client(server) as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat", "resolution": "1080p"})
    assert r.is_error
    assert "1080p 해상도를 허용하지 않습니다" in r.content[0].text


async def test_auto_duration_rejected_by_schema(make_app):
    server = create_server(make_app())
    async with Client(server) as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat", "duration_sec": "auto"})
    assert r.is_error


async def test_lifespan_collector_downloads_without_status_call(make_app, home):
    """상태 도구를 부르지 않아도 서버 lifespan 의 수거기가 파일을 받는다."""
    import asyncio

    app = make_app()
    server = create_server(app)
    async with Client(server) as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat", "duration_sec": 4})
        job_id = r.structured_content["job_id"]
        target = home / "output" / f"{job_id}.mp4"
        for _ in range(500):
            if target.exists():
                break
            await asyncio.sleep(0.01)
    assert target.exists()


async def test_tool_error_message_is_redacted(make_app):
    from capped_video_mcp.providers.base import ProviderError
    from capped_video_mcp.providers.mock import MockProvider

    from .conftest import FAKE_REPLICATE

    class Leaky(MockProvider):
        async def create_task(self, inp):
            raise ProviderError(f"replicate: 요청 거부. echo: {FAKE_REPLICATE}")

    server = create_server(make_app(provider=Leaky()))
    async with Client(server) as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat"})
    assert r.is_error
    assert FAKE_REPLICATE not in r.content[0].text
    assert "***" in r.content[0].text


async def test_estimate_via_protocol_is_free(make_app):
    app = make_app()
    server = create_server(app)
    async with Client(server) as c:
        r = await c.call_tool("estimate_video", {"duration_sec": 5})
        assert not r.is_error
        sc = r.structured_content
        assert sc["options"] and sc["cheapest"] == sc["options"][0]["model"]
        bad = await c.call_tool("estimate_video", {"model": "nope"})
        assert bad.is_error
    assert app.ledger.summary()["today_count"] == 0


async def test_elicitation_confirms_cost_via_protocol(make_app, settings):
    from mcp.types import ElicitResult
    app = make_app()
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"confirm_above_usd": 0.1})})
    server = create_server(app)
    seen = []

    async def accept(context, params):
        seen.append(params.message)
        return ElicitResult(action="accept", content={"proceed": True})

    async def decline(context, params):
        return ElicitResult(action="decline")

    # 인메모리 직접 연결에는 서버→클라이언트 요청 통로가 없어서 세션 모드(legacy)로 연다.
    async with Client(server, elicitation_callback=accept, mode="legacy") as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat"})
        assert not r.is_error and r.structured_content["status"] == "queued"
    assert seen and "$0.51" in seen[0] and "seedance-2.5" in seen[0]

    async with Client(server, elicitation_callback=decline, mode="legacy") as c:
        r = await c.call_tool("generate_video", {"prompt": "another boat"})
        assert r.is_error and "취소" in r.content[0].text
    assert app.ledger.summary()["today_count"] == 1


async def test_client_without_elicitation_follows_fallback(make_app, settings):
    app = make_app()
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(
        update={"confirm_above_usd": 0.1, "confirm_fallback": "deny"})})
    async with Client(create_server(app)) as c:        # 콜백 없음 → 능력 미선언
        r = await c.call_tool("generate_video", {"prompt": "a boat"})
        assert r.is_error and "지원하지 않습니다" in r.content[0].text
    app.settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"confirm_above_usd": 0.1})})
    async with Client(create_server(app)) as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat"})
        assert not r.is_error


async def test_preview_via_protocol_returns_image(make_app, monkeypatch, tmp_path):
    from capped_video_mcp import preview as P

    async def fake_run(args):
        return b"\xff\xd8\xff\xe0" + b"img" * 8 + b"\xff\xd9"
    monkeypatch.setattr(P, "_run_ffmpeg", fake_run)
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")
    app = make_app()
    app.settings = app.settings.model_copy(update={"preview": app.settings.preview.model_copy(update={"ffmpeg": str(fake)})})
    server = create_server(app)
    async with Client(server) as c:
        r = await c.call_tool("generate_video", {"prompt": "a boat", "duration_sec": 4})
        job_id = r.structured_content["job_id"]
        s = await c.call_tool("get_video_status", {"job_id": job_id, "wait_seconds": 30})
        assert s.structured_content["status"] == "succeeded"
        p = await c.call_tool("preview_video", {"job_id": job_id})
        assert not p.is_error
        kinds = [b.type for b in p.content]
        assert kinds == ["text", "image"] and p.content[1].mime_type == "image/jpeg"
        assert '"preview_available": true' in p.content[0].text
        missing = await c.call_tool("preview_video", {"job_id": "j_none"})
        assert missing.is_error
