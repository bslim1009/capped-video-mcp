"""Replicate 어댑터.

확인된 것 (문서):
- 공식 모델 생성: POST /v1/models/{owner}/{name}/predictions, 본문 {"input": {...}}
- 인증: Authorization: Bearer <REPLICATE_API_TOKEN>
- API 로 만든 결과는 완료 1시간 뒤 자동 삭제된다.

검증 필요 (작업진행.md "실제 API 검증" 단계):
- Seedance 2.5 입력 필드 중 해상도 필드 이름 (`resolution` 으로 가정)
- 상태 값 목록, output 형식 (문자열 / 배열)
- 영상 URL 도메인 (config.toml allowed_hosts)
"""

from __future__ import annotations

from typing import Any
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
    send,
)

API_BASE = "https://api.replicate.com/v1"
API_HOST = "api.replicate.com"

STATUS_MAP = {
    "starting": "queued",
    "processing": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "canceled": "failed",
    "aborted": "failed",
}


class ReplicateProvider:
    name = "replicate"

    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_task(self, inp: CreateInput) -> CreateResult:
        url = f"{API_BASE}/models/{inp.model_ref}/predictions"
        resp = await send(
            self._client, self.name, "POST", url, retry=False,
            headers=self._headers, json={"input": inp.payload},
        )
        body = create_body(resp, self.name)
        pred_id = body.get("id")
        if not isinstance(pred_id, str) or not pred_id:
            # 2xx 이므로 작업은 만들어졌을 수 있다 → uncertain (비용 포함)
            raise CreateUncertainError("replicate: 생성 응답에 예측 ID가 없습니다. 대시보드를 확인하세요.")
        urls = body.get("urls") if isinstance(body.get("urls"), dict) else {}
        # 다른 호스트의 URL 은 버리고 기본 API 경로를 쓴다 (작업은 정상으로 본다).
        remote_urls = {k: urls[k] for k in ("get", "cancel") if isinstance(urls.get(k), str) and _is_api_url(urls[k])}
        return CreateResult(remote_id=pred_id, remote_urls=remote_urls)

    def _get_url(self, job: dict[str, Any]) -> str:
        return job.get("remote_urls", {}).get("get") or f"{API_BASE}/predictions/{job['remote_id']}"

    async def get_task(self, job: dict[str, Any]) -> TaskState:
        resp = await send(self._client, self.name, "GET", self._get_url(job), retry=True, headers=self._headers)
        body = json_body(resp, self.name)
        raw = body.get("status")
        status = STATUS_MAP.get(raw)
        if status is None:
            raise ProviderError(f"replicate: 알 수 없는 상태 값 {raw!r}", retryable=True)
        if status == "succeeded":
            video_url = _first_url(body.get("output"))
            if not video_url:
                return TaskState("failed", error="replicate: 완료됐지만 결과 URL이 없습니다.")
            return TaskState("succeeded", video_url=video_url)
        if status == "failed":
            err = body.get("error") or raw
            return TaskState("failed", error=f"replicate: {str(err)[:200]}")
        return TaskState(status)

    async def cancel(self, job: dict[str, Any]) -> None:
        url = job.get("remote_urls", {}).get("cancel") or f"{API_BASE}/predictions/{job['remote_id']}/cancel"
        await send(self._client, self.name, "POST", url, retry=True, headers=self._headers)

    async def delete_remote(self, job: dict[str, Any]) -> None:
        # API 결과는 완료 1시간 뒤 자동 삭제되므로 따로 지우지 않는다.
        return None


def _is_api_url(url: str) -> bool:
    # 인증 헤더를 붙여 부를 URL 이므로 Replicate API 호스트만 허용한다.
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == API_HOST


def _first_url(output: Any) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return next((x for x in output if isinstance(x, str)), None)
    if isinstance(output, dict):
        for key in ("video", "url"):
            if isinstance(output.get(key), str):
                return output[key]
    return None
