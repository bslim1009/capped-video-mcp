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
Video generation server. generate_video is billed, so call it only when the user asks for a video.
After generating, check completion with get_video_status.
The server saves finished videos to its output folder automatically; get_video_status returns the file path.
Results are collected only while this server (session) is running, so avoid closing the session during a generation.
If an error says not to resend the same request ("다시 보내지 마세요"), do not repeat it (risk of double billing).
Models and prices: list_models. Remaining budget: get_usage.
To see what each model costs and which is cheapest within the budget, call estimate_video first (free).
Use preview_video (free) to get frames of a finished video and check that it matches the request."""

_EXPECTED = (PolicyError, AppError, LedgerError)
_ELICIT_FORM = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


class ConfirmCost(BaseModel):
    """Answer to a cost confirmation before generating."""

    proceed: bool = Field(description="true generates the video and bills it; false cancels.")


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
        image_path: str | None = None,
        reference_image_paths: list[str] | None = None,
        reference_audio_paths: list[str] | None = None,
        reference_dir: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a video generation job. **Billed.** Call only when the user asks for a video.

        Defaults come from config.toml (seedance-2.5 on Replicate, 480p, 5 s, 16:9).
        Image to video: pass image_path, the absolute path of a local image (PNG/JPEG/WebP) to use as the first frame.
        The model must then be an image-to-video model (alias ending in -i2v); if omitted, defaults.image_model from
        list_models is used. Most image-to-video models follow the image's aspect ratio, so do not pass aspect_ratio
        for them (their aspect_ratios in list_models is auto).
        Reference to video: builds a new scene from several images (characters, backgrounds) and audio. All paths are
        local absolute paths. Pass reference_image_paths (PNG/JPEG/WebP, at least one) and reference_audio_paths
        (MP3/WAV, each at least 1.8 s, 30 s in total), or reference_dir to use the images and audio in a folder in
        file-name order. Refer to them in the prompt as [Image1], [Image2], [Audio1] in that order (for example
        "The dinosaur from [Image1] fights the dinosaur from [Image2], roaring like [Audio1]"). Which file got which
        number can be checked first with estimate_video's references (free). The model is a reference-to-video model
        (alias ending in -ref); if omitted, defaults.reference_model is used. It cannot be combined with image_path.
        Each model allows different resolutions, durations and aspect ratios and has its own price; check list_models
        before using another model (e.g. veo takes 4/6/8 s, wan 5/10 s, hailuo 6/10 s only). Values you leave out are
        set to the cheapest ones the model allows. Automatic duration is not allowed. Requests over the daily or
        monthly budget are refused.
        Returns a job_id. When finished, the server saves the video to its output folder; check with get_video_status.
        If an error says not to resend the same request ("다시 보내지 마세요"), do not repeat it.
        If an identical request (prompt, model, resolution, duration, aspect ratio and input file contents) was
        accepted recently, no new job is created and the existing job_id is returned with duplicate=true (no charge).
        request_id also deduplicates. Use force=true only when the user wants another copy of the same video.
        If the first provider definitively rejects the request, the fallback model is used and failover_from names the
        original model. If the fallback costs more, the server asks the user directly and switches only on approval.
        Clients that cannot show that question get an error naming the fallback model and its price instead: ask the
        user, then retry with that model.
        If the estimated cost is at or above the configured threshold, the server may ask the user to confirm first.
        """
        async def confirm(message: str) -> bool | None:
            if ctx is None or not ctx.session.check_client_capability(_ELICIT_FORM):
                return None
            result = await ctx.elicit(message, schema=ConfirmCost)
            return result.action == "accept" and bool(result.data.proceed)

        try:
            return await app.generate(prompt, model, resolution, duration_sec, aspect_ratio,
                                      request_id=request_id, force=force, confirm=confirm, image_path=image_path,
                                      reference_image_paths=reference_image_paths,
                                      reference_audio_paths=reference_audio_paths, reference_dir=reference_dir)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_video_status(job_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        """Check a job's status. Not billed.

        Waits up to wait_seconds (max 60) for the job to finish.
        If status is queued/running/downloading, call this tool again.
        When it is succeeded, file holds the path of the saved video.
        """
        try:
            return await app.status(job_id, wait_seconds)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True, open_world_hint=False))
    async def download_video(job_id: str, wait_seconds: int = 30) -> dict[str, Any]:
        """Retry the download of a job whose video could not be downloaded (lost), or return the saved file path. Not billed.

        The server downloads results automatically, so this is rarely needed. It takes no URL or path.
        """
        try:
            return await app.download(job_id, wait_seconds)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def get_usage() -> dict[str, Any]:
        """Return today's and this month's generation count and estimated spend, the caps, and jobs in progress."""
        try:
            return app.usage()
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def preview_video(job_id: str) -> list[TextContent | ImageContent]:
        """Return several frames of a finished video tiled into one image. Not billed.

        You cannot watch the video, so use this image to check that the result matches the request. Frames run in
        time order from top left to bottom right. Only available once the job has succeeded. Without ffmpeg it
        returns preview_available=false and the reason.
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
        image_path: str | None = None,
        reference_image_paths: list[str] | None = None,
        reference_audio_paths: list[str] | None = None,
        reference_dir: str | None = None,
    ) -> dict[str, Any]:
        """Estimate cost without billing. Returns the models that fit the constraints (duration, resolution, aspect
        ratio), cheapest first, with the remaining budget.

        Use it before generate_video to pick a model or to check the budget. cheapest is the cheapest model within the
        budget. Constraints you leave out are set to each model's cheapest values. Models that cannot meet the
        constraints are listed in unavailable with the reason. With prompt, it also reports a recent identical request
        (duplicate_of).
        Models are compared within the same input kind: reference-to-video when reference files (reference_*) are
        given, image-to-video when image_path is given, otherwise text-to-video. With reference files, references maps
        the [Image1]/[Audio1] labels to the files.
        """
        try:
            return app.estimate(duration_sec, resolution, aspect_ratio, model, prompt, image_path,
                                reference_image_paths, reference_audio_paths, reference_dir)
        except _EXPECTED as e:
            raise tool_error(e) from e

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def list_models() -> dict[str, Any]:
        """Return the available models, per-second prices by resolution (USD), duration ranges and the defaults.

        Models whose input is image are image-to-video models and need image_path in generate_video.
        Models whose input is reference are reference-to-video models and need reference images
        (reference_image_paths or reference_dir).
        """
        return app.list_models()

    return mcp
