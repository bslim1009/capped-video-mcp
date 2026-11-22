"""개발·테스트용 가짜 제공처. 실제 API 를 부르지 않는다 (CAPPED_VIDEO_PROVIDER=mock).

- 상태 조회를 부를 때마다 queued → running → succeeded 로 진행한다.
- 프롬프트에 FAIL 이 있으면 failed, LOST 가 있으면 결과 URL 이 404 를 돌려준다.
- 영상은 mock_transport() 가 가짜 바이트로 응답한다 (재생되는 영상이 아니다).
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

import httpx

from .base import CreateInput, CreateResult, TaskState

if TYPE_CHECKING:
    from ..media import MediaFile

MOCK_HOST = "mock.invalid"
MOCK_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"MOCK-VIDEO" * 100


class MockProvider:
    name = "mock"

    def __init__(self, steps_to_finish: int = 2):
        self._seq = itertools.count(1)
        self._calls: dict[str, int] = {}
        self._prompts: dict[str, str] = {}
        self._steps = steps_to_finish
        self.cancelled: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        self.uploads: list[str] = []   # 올린 입력 파일의 sha256

    async def aclose(self) -> None:
        return None

    async def create_task(self, inp: CreateInput) -> CreateResult:
        rid = f"mock-{next(self._seq)}"
        self._calls[rid] = 0
        self._prompts[rid] = str(inp.payload.get("prompt", ""))
        self.payloads.append(dict(inp.payload))
        return CreateResult(remote_id=rid, remote_urls={})

    async def upload_file(self, media: MediaFile) -> str:
        self.uploads.append(media.sha256)
        return f"https://{MOCK_HOST}/inputs/{media.sha256[:12]}"

    async def get_task(self, job: dict[str, Any]) -> TaskState:
        rid = job["remote_id"]
        prompt = self._prompts.get(rid, "")
        n = self._calls.get(rid, self._steps)
        self._calls[rid] = n + 1
        if n == 0:
            return TaskState("queued")
        if n < self._steps:
            return TaskState("running")
        if "FAIL" in prompt:
            return TaskState("failed", error="mock: 의도된 실패")
        kind = "lost" if "LOST" in prompt else "ok"
        return TaskState("succeeded", video_url=f"https://{MOCK_HOST}/{kind}/{rid}.mp4")

    async def cancel(self, job: dict[str, Any]) -> None:
        self.cancelled.append(job["remote_id"])

    async def delete_remote(self, job: dict[str, Any]) -> None:
        return None


def mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != MOCK_HOST:
            return httpx.Response(502)
        if request.url.path.startswith("/lost/"):
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=MOCK_VIDEO_BYTES)

    return httpx.MockTransport(handler)
