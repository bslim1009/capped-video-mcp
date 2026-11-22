"""제공처 어댑터 공통 인터페이스와 HTTP 처리."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

log = logging.getLogger(__name__)

# 테스트에서 재시도 백오프를 짧게 바꿀 수 있게 따로 둔다.
_sleep = asyncio.sleep

TaskStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass(frozen=True)
class CreateInput:
    model_ref: str
    payload: dict[str, Any]   # 모델 입력 스키마에 맞춘 본문 (policy.build_payload)


@dataclass(frozen=True)
class CreateResult:
    remote_id: str
    remote_urls: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskState:
    status: TaskStatus
    video_url: str | None = None
    error: str | None = None


class ProviderError(Exception):
    """제공처 호출 실패. message 는 에이전트에게 보여도 되는 짧은 문장이다.

    retryable: 다음 수거 주기에 다시 시도해도 되는 일시적 오류.
    """

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


class CreateUncertainError(ProviderError):
    """원격 작업이 만들어졌는지(과금됐는지) 모른다.

    - 요청이 나간 뒤 연결이 끊기거나 응답 시간이 초과된 경우
    - 생성 요청이 5xx/408 로 실패한 경우 (게이트웨이 오류는 백엔드가 작업을 만든 뒤에도 난다)
    - 2xx 를 받았지만 응답 본문을 해석하지 못한 경우
    remote_id: 응답에서 원격 ID 를 얻었으면 담는다 (대시보드 확인용).
    """

    def __init__(self, message: str, *, remote_id: str | None = None, status_code: int | None = None):
        super().__init__(message, retryable=False, status_code=status_code)
        self.remote_id = remote_id


class Provider(Protocol):
    name: str

    async def create_task(self, inp: CreateInput) -> CreateResult: ...

    async def get_task(self, job: dict[str, Any]) -> TaskState: ...

    async def cancel(self, job: dict[str, Any]) -> None: ...

    async def delete_remote(self, job: dict[str, Any]) -> None: ...

    async def aclose(self) -> None: ...


def json_body(resp: httpx.Response, provider: str) -> dict[str, Any]:
    """상태 조회 응답을 JSON 으로 읽는다. 해석할 수 없으면 다음 주기에 다시 본다."""
    try:
        body = resp.json()
    except ValueError as e:
        raise ProviderError(f"{provider}: 응답을 해석하지 못했습니다.", retryable=True) from e
    if not isinstance(body, dict):
        raise ProviderError(f"{provider}: 예상하지 못한 응답 형식입니다.", retryable=True)
    return body


def create_body(resp: httpx.Response, provider: str) -> dict[str, Any]:
    """생성 응답(2xx)을 JSON 으로 읽는다. 2xx 는 작업이 만들어졌다는 뜻이므로 실패는 uncertain."""
    try:
        body = resp.json()
    except ValueError as e:
        raise CreateUncertainError(f"{provider}: 생성 응답을 해석하지 못했습니다. 대시보드를 확인하세요.") from e
    if not isinstance(body, dict):
        raise CreateUncertainError(f"{provider}: 생성 응답 형식이 예상과 다릅니다. 대시보드를 확인하세요.")
    return body


def short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def error_from_response(provider: str, resp: httpx.Response) -> ProviderError:
    code = resp.status_code
    # 응답 원문은 로그 파일에만 남긴다. 에이전트에게는 짧은 메시지만 준다.
    log.warning("%s HTTP %s: %s", provider, code, short(resp.text, 1000))
    if code in (401, 403):
        return ProviderError(f"{provider}: 인증 실패. API 키를 확인하세요.", status_code=code)
    if code == 402:
        return ProviderError(f"{provider}: 크레딧이 부족합니다.", status_code=code)
    if code == 429:
        return ProviderError(f"{provider}: 요청이 너무 많습니다. 잠시 뒤 다시 시도하세요.", retryable=True, status_code=code)
    if code >= 500:
        return ProviderError(f"{provider}: 서버 오류(HTTP {code}).", retryable=True, status_code=code)
    detail = _detail(resp)
    return ProviderError(f"{provider}: 요청 거부(HTTP {code}). {detail}".strip(), status_code=code)


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        for key in ("detail", "error", "message", "title"):
            if body.get(key):
                return short(body[key] if isinstance(body[key], str) else str(body[key]))
    return ""


async def send(
    client: httpx.AsyncClient,
    provider: str,
    method: str,
    url: str,
    *,
    retry: bool,
    **kwargs: Any,
) -> httpx.Response:
    """HTTP 요청. retry=True 이면 429/5xx/네트워크 오류를 최대 2번 재시도한다.

    생성 요청(POST)은 중복 과금 위험이 있어 retry=False 로 부른다.
    """
    attempts = 3 if retry else 1
    for i in range(attempts):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # 연결 전에 실패 → 요청이 전달되지 않았다.
            if i + 1 < attempts:
                await _sleep(2**i)
                continue
            raise ProviderError(f"{provider}: 연결할 수 없습니다.", retryable=True) from e
        except httpx.TransportError as e:
            # 연결 후 실패(응답 시간 초과, 연결 끊김) → 요청이 전달됐을 수 있다.
            if i + 1 < attempts:
                await _sleep(2**i)
                continue
            if not retry:
                raise CreateUncertainError(
                    f"{provider}: 생성 요청의 응답을 받지 못했습니다. 원격 작업이 만들어졌을 수 있으니 대시보드를 확인하세요.",
                ) from e
            raise ProviderError(f"{provider}: 네트워크 오류 또는 응답 시간 초과.", retryable=True) from e
        if resp.status_code < 400:
            return resp
        err = error_from_response(provider, resp)
        if not retry and (resp.status_code >= 500 or resp.status_code == 408):
            # 생성 요청의 서버 오류는 작업이 만들어졌는지 알 수 없다 → 과금된 것으로 본다.
            raise CreateUncertainError(
                f"{provider}: 생성 요청에 서버 오류(HTTP {resp.status_code})가 났습니다. "
                "원격 작업이 만들어졌을 수 있으니 대시보드를 확인하세요.",
                status_code=resp.status_code,
            )
        if err.retryable and i + 1 < attempts:
            await _sleep(2**i)
            continue
        raise err
    raise AssertionError("unreachable")
