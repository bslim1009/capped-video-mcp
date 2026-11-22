"""MCP 도구 정의 (mcp SDK 2.x: MCPServer)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from .ledger import LedgerError
from .log import redact
from .policy import PolicyError
from .service import App, AppError

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
영상 생성 서버입니다. generate_video 는 과금되므로 사용자가 요청할 때만 부르세요.
생성 후에는 get_video_status 로 완료를 확인합니다.
완료된 영상은 서버가 출력 폴더에 자동 저장하고, get_video_status 가 파일 경로를 알려 줍니다.
결과 수거는 이 서버(세션)가 켜져 있는 동안에만 일어납니다. 생성 중에는 세션을 닫지 않는 것이 좋습니다.
오류 메시지가 "다시 보내지 마세요"라고 하면 같은 요청을 반복하지 마세요 (이중 과금 위험).
모델·가격은 list_models, 남은 예산은 get_usage 로 확인합니다.
어떤 모델이 얼마인지, 예산 안에서 가장 싼 모델이 무엇인지는 estimate_video (무료) 로 먼저 봅니다.
완성된 영상은 preview_video (무료) 로 프레임 이미지를 받아 결과가 요청과 맞는지 눈으로 확인합니다."""

_EXPECTED = (PolicyError, AppError, LedgerError)
_ELICIT_FORM = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


class ConfirmCost(BaseModel):
    """생성 전 비용 확인 응답."""

    proceed: bool = Field(description="true 면 생성을 진행하고 과금됩니다. false 면 취소합니다.")


def create_server(app: App) -> MCPServer:
    secrets = app.secrets.values()

    def tool_error(e: Exception) -> ToolError:
        # 제공처 응답 일부가 메시지에 들어갈 수 있으므로 에이전트에게 보내기 전에 키를 가린다.
        return ToolError(redact(str(e), secrets))

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[dict[str, Any]]:
        task = asyncio.create_task(app.collector.run_forever())
        try:
            yield {}
        finally:
            task.cancel()
            # 종료 중 바깥 취소가 와도 정리는 끝까지 한다.
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                with contextlib.suppress(Exception):
                    pending = app.pending_job_ids()
                    if pending:
                        log.warning("서버 종료: 끝나지 않은 작업 %d개 %s. 다음에 서버가 뜨면 이어서 수거합니다.",
                                    len(pending), pending)
                await app.aclose()

    mcp = MCPServer("capped-video", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=False, open_world_hint=True))
    async def generate_video(
        prompt: str,
        model: str | None = None,
        resolution: str | None = None,
        duration_sec: int | None = None,
        aspect_ratio: str | None = None,
        request_id: str | None = None,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """영상 생성 작업을 만든다. **과금된다.** 사용자가 영상 생성을 요청했을 때만 부른다.

        기본값: config.toml 의 defaults (seedance-2.5 / Replicate, 480p, 5초, 16:9).
        모델마다 허용 해상도·길이·화면 비율과 가격이 다르다. 다른 모델을 쓰기 전에 list_models 로 확인한다
        (예: veo 는 4·6·8초, wan 은 5·10초, hailuo 는 6·10초만). 지정하지 않으면 모델에 맞는 가장 싼 값을 고른다.
        자동 길이는 허용하지 않는다. 일일·월간 예산을 넘으면 거부한다.
        job_id 를 반환한다. 완료되면 서버가 출력 폴더에 자동 저장하며, get_video_status 로 확인한다.
        오류가 "다시 보내지 마세요"라고 하면 같은 요청을 반복하지 않는다.
        같은 요청(프롬프트·모델·해상도·길이·비율)이 최근에 이미 접수됐으면 새 작업을 만들지 않고 기존 job_id 를
        duplicate=true 로 돌려준다 (과금 없음). request_id 를 주면 그 값으로도 중복을 잡는다.
        사용자가 같은 영상을 하나 더 원할 때만 force=true 를 쓴다.
        1차 제공처가 요청을 확정 거부하면 대체 모델로 만들고 failover_from 에 원래 모델을 적는다.
        예상 비용이 설정 임계값 이상이면 서버가 사용자에게 직접 확인을 요청할 수 있다.
        """
        async def confirm(req) -> bool | None:
            if ctx is None or not ctx.session.check_client_capability(_ELICIT_FORM):
                return None
            result = await ctx.elicit(
                f"영상 생성에 약 ${req.estimated_usd:.2f} 가 과금됩니다 ({req.model.alias}, {req.resolution}, "
                f"{req.duration_sec}초, {req.aspect_ratio}). 진행할까요?",
                schema=ConfirmCost,
            )
            return result.action == "accept" and bool(result.data.proceed)

        try:
            return await app.generate(prompt, model, resolution, duration_sec, aspect_ratio,
                                      request_id=request_id, force=force, confirm=confirm)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_video_status(job_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        """작업 상태를 확인한다. 과금되지 않는다.

        완료될 때까지 최대 wait_seconds(상한 60초) 동안 기다린다.
        status 가 queued/running/downloading 이면 이 도구를 다시 부른다.
        succeeded 이면 file 에 저장된 영상 경로가 있다.
        """
        try:
            return await app.status(job_id, wait_seconds)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True, open_world_hint=False))
    async def download_video(job_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        """내려받지 못한(lost) 작업의 다운로드를 다시 시도하거나, 완료된 파일 경로를 돌려준다. 과금되지 않는다.

        보통은 서버가 자동으로 내려받으므로 부를 필요가 없다. URL 이나 경로는 받지 않는다.
        """
        try:
            return await app.download(job_id, wait_seconds)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def get_usage() -> dict[str, Any]:
        """오늘·이번 달 생성 횟수와 예상 비용, 상한, 진행 중인 작업 수를 돌려준다."""
        try:
            return app.usage()
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def preview_video(job_id: str) -> list[TextContent | ImageContent]:
        """완성된 영상의 프레임 여러 장을 한 이미지로 붙여 돌려준다. 과금되지 않는다.

        영상을 직접 볼 수 없으니 이 이미지로 결과가 요청과 맞는지 확인한다. 왼쪽 위부터 오른쪽 아래로 시간 순서다.
        succeeded 상태에서만 만들 수 있다. ffmpeg 가 없으면 preview_available=false 와 이유를 돌려준다.
        """
        try:
            view, image = await app.preview(job_id)
        except _EXPECTED as e:
            raise tool_error(e) from e
        out: list[TextContent | ImageContent] = [TextContent(type="text", text=json.dumps(view, ensure_ascii=False, indent=2))]
        if image is not None:
            out.append(ImageContent(type="image", data=base64.b64encode(image).decode(), mime_type="image/jpeg"))
        return out

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def estimate_video(
        duration_sec: int | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """과금 없이 비용을 견적한다. 조건(길이·해상도·화면 비율)에 맞는 모델을 싼 순서로, 남은 예산과 함께 돌려준다.

        generate_video 를 부르기 전에 모델을 고르거나 예산이 충분한지 볼 때 쓴다. cheapest 는 예산 안에서 가장 싼 모델.
        조건을 지정하지 않으면 모델별 가장 싼 값을 가정한다. 조건을 만족하지 못하는 모델은 unavailable 에 이유와 함께 나온다.
        prompt 를 주면 같은 요청이 최근에 있었는지(duplicate_of)도 알려 준다.
        """
        try:
            return app.estimate(duration_sec, resolution, aspect_ratio, model, prompt)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def list_models() -> dict[str, Any]:
        """사용할 수 있는 모델, 해상도별 초당 가격(USD), 길이 범위, 기본값을 돌려준다."""
        return app.list_models()

    return mcp
