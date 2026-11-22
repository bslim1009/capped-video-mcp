"""fal 어댑터 (큐 API).

확인된 것 (fal 문서, 2026-11-21):
- 생성: POST https://queue.fal.run/{model_id}, 입력값은 본문 최상위
- 인증: Authorization: Key <FAL_KEY>
- 생성 응답: request_id, status_url, response_url, cancel_url. URL 은 직접 만들지 말고 그대로 쓴다.
- 상태: IN_QUEUE, IN_PROGRESS, COMPLETED. 실패·취소도 COMPLETED → 결과를 받아 봐야 안다.
- X-Fal-Store-IO: 0 헤더로 입출력 보관을 끈다.
- 입력 파일 업로드 (file-access-controls 문서): POST rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3
  본문 {file_name, content_type} → {upload_url, file_url}. upload_url 에 바이트를 PUT 하고 file_url 을 입력으로 쓴다.
  보관 기간은 X-Fal-Object-Lifecycle-Preference: {"expiration_duration_seconds": N} 으로 정한다.
  data URI 는 "몇 KB 넘는 파일에는 권장하지 않는다"(fal-cdn 문서)라서 쓰지 않는다.
- Seedance 2.5 입력: prompt, resolution("480p"...), duration("4"~"30" 문자열, 기본 "auto"),
  aspect_ratio, generate_audio. 결과: {"video": {"url": ...}}

검증 필요:
- COMPLETED 인데 실패한 경우 response_url 응답 형식
- cancel 메서드 (PUT 으로 가정)
- 영상 URL 도메인 (config.toml allowed_hosts)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .base import (
    CreateInput,
    CreateResult,
    CreateUncertainError,
    ProviderError,
    TaskState,
    create_body,
    json_body,
    media_file_name,
    send,
)

if TYPE_CHECKING:
    from ..media import MediaFile

QUEUE_BASE = "https://queue.fal.run"
QUEUE_HOST = "queue.fal.run"
UPLOAD_INITIATE_URL = "https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3"
# 올린 입력 파일의 CDN 보관 기간. 대기열·생성이 끝날 때까지는 남아 있어야 한다.
UPLOAD_EXPIRATION_SEC = 86_400

STATUS_MAP = {"IN_QUEUE": "queued", "IN_PROGRESS": "running", "COMPLETED": "completed"}


class FalProvider:
    name = "fal"

    def __init__(self, key: str, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._headers = {
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
            "X-Fal-Store-IO": "0",  # 입출력을 fal 에 보관하지 않는다
        }
        self._upload_headers = {
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
            "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": UPLOAD_EXPIRATION_SEC}),
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_task(self, inp: CreateInput) -> CreateResult:
        url = f"{QUEUE_BASE}/{inp.model_ref}"
        resp = await send(
            self._client, self.name, "POST", url, retry=False,
            headers=self._headers, json=inp.payload,   # fal 은 입력값을 본문 최상위에 둔다
        )
        body = create_body(resp, self.name)
        req_id = body.get("request_id")
        if not isinstance(req_id, str) or not req_id:
            # 2xx 이므로 작업은 만들어졌을 수 있다 → uncertain (비용 포함)
            raise CreateUncertainError("fal: 생성 응답에 request_id 가 없습니다. 대시보드를 확인하세요.")
        urls = {}
        for key, name in (("status_url", "status"), ("response_url", "response"), ("cancel_url", "cancel")):
            value = body.get(key)
            if isinstance(value, str) and _is_queue_url(value):
                urls[name] = value
        if "status" not in urls or "response" not in urls:
            # 키를 다른 호스트로 보낼 수 없으므로 수거는 못 하지만, 작업은 만들어졌다 → uncertain
            raise CreateUncertainError(
                f"fal: 상태·결과 URL이 없거나 허용되지 않은 호스트입니다. 대시보드에서 요청 {req_id} 를 확인하세요.",
                remote_id=req_id,
            )
        return CreateResult(remote_id=req_id, remote_urls=urls)

    async def upload_file(self, media: MediaFile) -> str:
        resp = await send(
            self._client, self.name, "POST", UPLOAD_INITIATE_URL, retry=True,
            headers=self._upload_headers, json={"file_name": media_file_name(media), "content_type": media.mime},
        )
        body = json_body(resp, self.name)
        upload_url, file_url = body.get("upload_url"), body.get("file_url")
        if not (_is_https(upload_url) and _is_https(file_url)):
            raise ProviderError("fal: 업로드 응답에 upload_url/file_url 이 없습니다.")
        # presigned URL 이라 키를 보내지 않는다. 오류는 키 문제가 아니므로 따로 안내한다 (403 = 서명 만료 등).
        try:
            put = await send(self._client, self.name, "PUT", upload_url, retry=True,
                             headers={"Content-Type": media.mime}, content=media.data)
        except ProviderError as e:
            code = f"HTTP {e.status_code}" if e.status_code else "네트워크 오류"
            raise ProviderError(f"fal: 입력 파일 전송 실패 ({code}).", retryable=e.retryable, status_code=e.status_code) from e
        if not 200 <= put.status_code < 300:   # 3xx 는 올라가지 않은 것이다 (redirect 를 따라가지 않음)
            raise ProviderError(f"fal: 입력 파일 전송 실패 (HTTP {put.status_code}).", status_code=put.status_code)
        return file_url

    async def get_task(self, job: dict[str, Any]) -> TaskState:
        urls = job.get("remote_urls", {})
        resp = await send(self._client, self.name, "GET", urls["status"], retry=True, headers=self._headers)
        raw = json_body(resp, self.name).get("status")
        status = STATUS_MAP.get(raw)
        if status is None:
            raise ProviderError(f"fal: 알 수 없는 상태 값 {raw!r}", retryable=True)
        if status != "completed":
            return TaskState(status)

        # COMPLETED: 성공인지 실패인지 결과로 판단한다.
        try:
            result = await send(self._client, self.name, "GET", urls["response"], retry=True, headers=self._headers)
        except ProviderError as e:
            if e.retryable:
                raise
            return TaskState("failed", error=e.message)
        body = json_body(result, self.name)
        video = body.get("video")
        video_url = video.get("url") if isinstance(video, dict) else None
        if isinstance(video_url, str) and video_url:
            return TaskState("succeeded", video_url=video_url)
        detail = body.get("detail")
        return TaskState("failed", error=f"fal: 결과에 영상이 없습니다. {str(detail or '')[:200]}".strip())

    async def cancel(self, job: dict[str, Any]) -> None:
        url = job.get("remote_urls", {}).get("cancel")
        if url:
            await send(self._client, self.name, "PUT", url, retry=True, headers=self._headers)

    async def delete_remote(self, job: dict[str, Any]) -> None:
        # CDN 파일 삭제는 Platform API(ADMIN 범위 키 필요 가능성)가 필요하다.
        # 생성 서버에는 API 범위 키만 두므로 삭제하지 않고 X-Fal-Store-IO: 0 에 의존한다.
        return None


def _is_https(url: Any) -> bool:
    return isinstance(url, str) and urlsplit(url).scheme == "https" and bool(urlsplit(url).hostname)


def _is_queue_url(url: str) -> bool:
    # 인증 헤더(키)를 붙여 부를 URL 이므로 fal 큐 호스트만 허용한다.
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == QUEUE_HOST
