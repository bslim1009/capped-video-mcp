import json

import httpx
import pytest

from capped_video_mcp.providers.base import CreateInput, CreateUncertainError, ProviderError
from capped_video_mcp.providers.fal import FalProvider
from capped_video_mcp.providers.replicate import ReplicateProvider

from .conftest import FAKE_FAL, FAKE_REPLICATE, FIXTURES


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# 요청 본문은 policy.build_payload 가 만든다 (test_policy 에서 모델별로 검증). 어댑터는 그대로 보내기만 한다.
INP = CreateInput(model_ref="bytedance/seedance-2.5",
                  payload={"prompt": "a boat", "duration": 5, "resolution": "480p", "aspect_ratio": "16:9",
                           "generate_audio": True})
FAL_INP = CreateInput(model_ref="bytedance/seedance-2.5/text-to-video",
                      payload={"prompt": "a boat", "duration": "5", "resolution": "480p", "aspect_ratio": "16:9",
                               "generate_audio": True})


class Recorder:
    """요청을 기록하고 순서대로 응답(또는 예외)을 돌려주는 MockTransport."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


# ── Replicate ──

async def test_replicate_create():
    rec = Recorder(httpx.Response(201, json=load("replicate/create.json")))
    p = ReplicateProvider(FAKE_REPLICATE, rec.client())
    res = await p.create_task(INP)
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.replicate.com/v1/models/bytedance/seedance-2.5/predictions"
    assert req.headers["Authorization"] == f"Bearer {FAKE_REPLICATE}"
    body = json.loads(req.content)
    assert body == {"input": INP.payload}                # Replicate 는 input 으로 감싼다
    assert res.remote_id == "pred_abc123"
    assert res.remote_urls["get"].startswith("https://api.replicate.com/")


async def test_replicate_drops_foreign_urls():
    body = load("replicate/create.json")
    body["urls"]["get"] = "https://evil.example.com/steal"
    rec = Recorder(httpx.Response(201, json=body))
    res = await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)
    assert "get" not in res.remote_urls  # 인증 헤더를 다른 호스트로 보내지 않는다


@pytest.mark.parametrize(
    "body, status, url",
    [
        ({"status": "starting"}, "queued", None),
        ({"status": "processing"}, "running", None),
        ({"status": "succeeded", "output": "https://replicate.delivery/a.mp4"}, "succeeded", "https://replicate.delivery/a.mp4"),
        ({"status": "succeeded", "output": ["https://replicate.delivery/b.mp4"]}, "succeeded", "https://replicate.delivery/b.mp4"),
        ({"status": "succeeded", "output": None}, "failed", None),
        ({"status": "canceled"}, "failed", None),
    ],
)
async def test_replicate_status_mapping(body, status, url):
    rec = Recorder(httpx.Response(200, json=body))
    st = await ReplicateProvider(FAKE_REPLICATE, rec.client()).get_task({"remote_id": "x", "remote_urls": {}})
    assert st.status == status
    assert st.video_url == url
    assert str(rec.requests[0].url) == "https://api.replicate.com/v1/predictions/x"


async def test_replicate_failed_has_error():
    rec = Recorder(httpx.Response(200, json=load("replicate/failed.json")))
    st = await ReplicateProvider(FAKE_REPLICATE, rec.client()).get_task({"remote_id": "x", "remote_urls": {}})
    assert st.status == "failed" and "moderation" in st.error


async def test_replicate_unknown_status_is_retryable_error():
    rec = Recorder(httpx.Response(200, json={"status": "weird"}))
    with pytest.raises(ProviderError) as e:
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).get_task({"remote_id": "x", "remote_urls": {}})
    assert e.value.retryable


async def test_auth_error_message_has_no_secret():
    rec = Recorder(httpx.Response(401, json={"detail": "Invalid token"}))
    with pytest.raises(ProviderError) as e:
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)
    assert "API 키" in e.value.message and FAKE_REPLICATE not in e.value.message
    assert not e.value.retryable


async def test_get_retries_on_429_then_succeeds():
    rec = Recorder(httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"status": "processing"}))
    st = await ReplicateProvider(FAKE_REPLICATE, rec.client()).get_task({"remote_id": "x", "remote_urls": {}})
    assert st.status == "running" and len(rec.requests) == 3


@pytest.mark.parametrize("code", [500, 502, 504, 408])
async def test_create_5xx_is_not_retried_and_is_uncertain(code):
    rec = Recorder(httpx.Response(code), httpx.Response(201, json=load("replicate/create.json")))
    with pytest.raises(CreateUncertainError):
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)
    assert len(rec.requests) == 1  # 중복 과금 방지


@pytest.mark.parametrize("code", [400, 402, 422, 429])
async def test_create_4xx_is_definite_rejection(code):
    rec = Recorder(httpx.Response(code, json={"detail": "bad"}))
    with pytest.raises(ProviderError) as e:
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)
    assert not isinstance(e.value, CreateUncertainError)
    assert len(rec.requests) == 1


async def test_create_2xx_without_id_is_uncertain():
    rec = Recorder(httpx.Response(201, json={"status": "starting"}))
    with pytest.raises(CreateUncertainError):
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)


async def test_create_2xx_not_json_is_uncertain():
    rec = Recorder(httpx.Response(201, content=b"<html>oops"))
    with pytest.raises(CreateUncertainError):
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)


async def test_status_not_json_is_retryable():
    rec = Recorder(httpx.Response(200, content=b"<html>"))
    with pytest.raises(ProviderError) as e:
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).get_task({"remote_id": "x", "remote_urls": {}})
    assert e.value.retryable


async def test_create_timeout_is_uncertain():
    rec = Recorder(httpx.ReadTimeout("timeout"))
    with pytest.raises(CreateUncertainError):
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)


async def test_create_connect_error_is_not_uncertain():
    rec = Recorder(httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as e:
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).create_task(INP)
    assert not isinstance(e.value, CreateUncertainError)


# ── fal ──

async def test_fal_create_headers_and_body():
    rec = Recorder(httpx.Response(200, json=load("fal/create.json")))
    res = await FalProvider(FAKE_FAL, rec.client()).create_task(FAL_INP)
    req = rec.requests[0]
    assert str(req.url) == "https://queue.fal.run/bytedance/seedance-2.5/text-to-video"
    assert req.headers["Authorization"] == f"Key {FAKE_FAL}"
    assert req.headers["X-Fal-Store-IO"] == "0"
    assert json.loads(req.content) == FAL_INP.payload   # fal 은 입력값을 본문 최상위에 그대로
    assert res.remote_id == "764cabcf-b745-4b3e-ae38-1200304cf45b"
    assert set(res.remote_urls) == {"status", "response", "cancel"}


async def test_fal_foreign_status_url_is_uncertain_with_remote_id():
    """2xx 는 작업이 만들어졌다는 뜻 → 키는 보내지 않되 비용은 잡는다(uncertain)."""
    body = load("fal/create.json")
    body["status_url"] = "https://evil.example.com/status"
    rec = Recorder(httpx.Response(200, json=body))
    with pytest.raises(CreateUncertainError) as e:
        await FalProvider(FAKE_FAL, rec.client()).create_task(FAL_INP)
    assert e.value.remote_id == body["request_id"]
    assert len(rec.requests) == 1  # evil 호스트로 요청하지 않는다


def _fal_job():
    c = load("fal/create.json")
    return {"remote_id": c["request_id"],
            "remote_urls": {"status": c["status_url"], "response": c["response_url"], "cancel": c["cancel_url"]}}


async def test_fal_in_queue_uses_given_status_url():
    rec = Recorder(httpx.Response(200, json={"status": "IN_QUEUE", "queue_position": 2}))
    st = await FalProvider(FAKE_FAL, rec.client()).get_task(_fal_job())
    assert st.status == "queued"
    assert str(rec.requests[0].url) == _fal_job()["remote_urls"]["status"]
    assert rec.requests[0].headers["X-Fal-Store-IO"] == "0"


async def test_fal_completed_success():
    rec = Recorder(httpx.Response(200, json={"status": "COMPLETED"}), httpx.Response(200, json=load("fal/result.json")))
    st = await FalProvider(FAKE_FAL, rec.client()).get_task(_fal_job())
    assert st.status == "succeeded" and st.video_url.endswith("output.mp4")


async def test_fal_completed_but_failed():
    rec = Recorder(httpx.Response(200, json={"status": "COMPLETED"}),
                   httpx.Response(422, json={"detail": "prompt rejected"}))
    st = await FalProvider(FAKE_FAL, rec.client()).get_task(_fal_job())
    assert st.status == "failed" and "prompt rejected" in st.error


async def test_fal_completed_result_5xx_is_retryable():
    rec = Recorder(httpx.Response(200, json={"status": "COMPLETED"}),
                   httpx.Response(500), httpx.Response(500), httpx.Response(500))
    with pytest.raises(ProviderError) as e:
        await FalProvider(FAKE_FAL, rec.client()).get_task(_fal_job())
    assert e.value.retryable


async def test_fal_cancel_uses_cancel_url():
    rec = Recorder(httpx.Response(202, json={}))
    await FalProvider(FAKE_FAL, rec.client()).cancel(_fal_job())
    assert rec.requests[0].method == "PUT"
    assert str(rec.requests[0].url) == _fal_job()["remote_urls"]["cancel"]


@pytest.mark.parametrize(("code", "body", "expect"), [
    (403, {"detail": "User is locked. Reason: Exhausted balance. Top up your balance at fal.ai/dashboard/billing."}, "잔액"),
    (403, {"detail": "Forbidden"}, "API 키"),
    (402, {"title": "Insufficient credit"}, "잔액"),
])
async def test_balance_vs_auth_errors(code, body, expect):
    """fal 은 잔액 0 을 403 으로 보낸다 (2026-11-22 실제 응답). 키 문제로 안내하면 안 된다."""
    rec = Recorder(httpx.Response(code, json=body))
    with pytest.raises(ProviderError) as e:
        await FalProvider(FAKE_FAL, rec.client()).create_task(FAL_INP)
    assert expect in e.value.message and not e.value.retryable and FAKE_FAL not in e.value.message
