"""이미지→영상: 이미지 파일 검증, 모델별 요청 본문, 업로드, 중복 감지, 제공처 전환.

모델별 기대값은 제공처 입력 스키마(fal llms.txt·OpenAPI, Replicate api/schema, 2026-11-22)에서 옮겨 적었다.
"""

import asyncio
import hashlib
import json

import httpx
import pytest
from mcp import Client
from pydantic import ValidationError

from capped_video_mcp import ledger as L
from capped_video_mcp import service
from capped_video_mcp.config import ModelConfig, Settings
from capped_video_mcp.media import load_image
from capped_video_mcp.policy import PolicyError, UploadedInputs, build_payload, build_request
from capped_video_mcp.providers.base import ProviderError
from capped_video_mcp.providers.fal import FalProvider
from capped_video_mcp.providers.mock import MockProvider
from capped_video_mcp.providers.replicate import ReplicateProvider
from capped_video_mcp.server import create_server
from capped_video_mcp.service import AppError

from .conftest import FAKE_FAL, FAKE_REPLICATE

P = "the boat drifts away"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
URL = "https://cdn.example/input.png"


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "frame.png"
    path.write_bytes(PNG)
    return path


@pytest.fixture
def img(png):
    return load_image(str(png), 10_000_000)


# ── 이미지 파일 검증 ──

@pytest.mark.parametrize("data, mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_load_image_detects_format_by_content(tmp_path, data, mime):
    path = tmp_path / "x.bin"          # 확장자가 아니라 내용으로 판단한다
    path.write_bytes(data)
    image = load_image(str(path), 10_000)
    assert image.mime == mime
    assert image.sha256 == hashlib.sha256(data).hexdigest() and image.data == data
    assert "data=" not in repr(image)          # repr(로그)에 바이트가 나오지 않는다


def test_load_image_does_not_expand_env_vars(monkeypatch, tmp_path):
    """에이전트가 준 경로의 환경 변수를 풀면 키가 오류 메시지로 샌다."""
    monkeypatch.setenv("FAL_KEY", "secret-abc123")
    for raw in (str(tmp_path / "%FAL_KEY%.png"), str(tmp_path / "$FAL_KEY.png"), str(tmp_path / "${FAL_KEY}.png")):
        with pytest.raises(PolicyError) as e:
            load_image(raw, 10_000)
        assert "secret-abc123" not in str(e.value) and "FAL_KEY" in str(e.value)


@pytest.mark.parametrize("raw", [r"\\attacker.example\share\a.png", "//attacker.example/share/a.png",
                                 r"\\?\UNC\attacker.example\share\a.png"])
def test_load_image_rejects_network_paths(raw):
    with pytest.raises(PolicyError, match="네트워크 경로"):
        load_image(raw, 10_000)


def test_load_image_rejections(tmp_path, png):
    (tmp_path / "empty.png").write_bytes(b"")
    (tmp_path / "anim.gif").write_bytes(b"GIF89a" + b"\x00" * 20)
    cases = [
        ("", "비어 있습니다"),
        ("frame.png", "절대 경로"),
        (str(tmp_path / "missing.png"), "파일이 없습니다"),
        (str(tmp_path), "파일이 없습니다"),
        (str(tmp_path / "empty.png"), "비어 있습니다"),
        (str(tmp_path / "anim.gif"), "PNG, JPEG, WebP"),
    ]
    for path, msg in cases:
        with pytest.raises(PolicyError, match=msg):
            load_image(path, 10_000)
    with pytest.raises(PolicyError, match="너무 큽니다"):
        load_image(str(png), len(PNG) - 1)


# ── 모델별 요청 본문과 비용 ──

@pytest.mark.parametrize(
    "alias, kwargs, expected, usd",
    [
        # Replicate: 같은 모델에 image. 첫 프레임 모드는 aspect_ratio "adaptive" 필수
        ("seedance-2.5-i2v", {}, {"prompt": P, "duration": 5, "resolution": "480p", "generate_audio": True,
                                  "image": URL, "aspect_ratio": "adaptive"}, 0.514),
        # fal Seedance: image_url, aspect_ratio 는 image-to-video 에서 항상 "auto"
        ("seedance-2.5-fal-i2v", {}, {"prompt": P, "duration": "5", "resolution": "480p", "generate_audio": True,
                                      "image_url": URL, "aspect_ratio": "auto"}, 1.1025),
        # Kling: start_image_url, 해상도·비율 필드 없음
        ("kling-3.0-i2v", {}, {"prompt": P, "duration": "5", "generate_audio": True, "start_image_url": URL}, 0.63),
        # Wan: 비율·소리 필드 없음, 해상도는 기본 1080p 라 명시
        ("wan-2.5-i2v", {}, {"prompt": P, "duration": "5", "resolution": "480p", "image_url": URL}, 0.25),
        # Veo Fast: 비율 기본 auto 라 보내지 않음
        ("veo-3.1-fast-i2v", {}, {"prompt": P, "duration": "4s", "resolution": "720p", "generate_audio": True,
                                  "image_url": URL}, 0.6),
        ("veo-3.1-lite-i2v", {"duration_sec": 8}, {"prompt": P, "duration": "8s", "resolution": "720p",
                                                    "generate_audio": True, "image_url": URL}, 0.4),
        # Hailuo: 768p 고정, 한 편당
        ("hailuo-2.3-i2v", {"duration_sec": 10}, {"prompt": P, "duration": "10", "image_url": URL}, 0.56),
        # Gemini: 비율 16:9·9:16 선택 (auto 없음)
        ("gemini-omni-1.2-flash-i2v", {"aspect_ratio": "9:16"},
         {"prompt": P, "duration": 5, "resolution": "360p", "aspect_ratio": "9:16", "image_url": URL}, 0.1365),
    ],
)
def test_image_payload_and_cost(settings, img, alias, kwargs, expected, usd):
    req = build_request(settings, P, model=alias, image=img, **kwargs)
    assert build_payload(req, UploadedInputs(image=URL)) == expected
    assert req.estimated_usd == pytest.approx(usd)


def test_default_image_model_when_model_omitted(settings, img):
    req = build_request(settings, P, image=img)
    assert req.model.alias == "seedance-2.5-i2v" and req.aspect_ratio == "auto"
    assert build_request(settings, P).model.alias == "seedance-2.5"   # 이미지가 없으면 텍스트 기본 모델


def test_image_policy_rejections(settings, img):
    with pytest.raises(PolicyError, match="텍스트→영상 모델이라 이미지·참조 파일을 받지 않습니다.*kling-3.0-i2v"):
        build_request(settings, P, model="kling-3.0", image=img)
    with pytest.raises(PolicyError, match="image_path"):
        build_request(settings, P, model="kling-3.0-i2v")
    with pytest.raises(PolicyError, match="화면 비율을 따릅니다"):
        build_request(settings, P, model="kling-3.0-i2v", image=img, aspect_ratio="16:9")
    with pytest.raises(PolicyError, match="화면 비율"):
        build_request(settings, P, model="gemini-omni-1.2-flash-i2v", image=img, aspect_ratio="1:1")
    # 빈 값은 텍스트 모델처럼 '지정 안 함'으로 본다
    assert build_request(settings, P, model="kling-3.0-i2v", image=img, aspect_ratio="").aspect_ratio == "auto"
    no_default = settings.model_copy(update={"defaults": settings.defaults.model_copy(update={"image_model": None})})
    with pytest.raises(PolicyError, match="model 에 이미지→영상 모델을 지정"):
        build_request(no_default, P, image=img)


def test_payload_requires_image_url(settings, img):
    with pytest.raises(ValueError):
        build_payload(build_request(settings, P, model="wan-2.5-i2v", image=img))


def test_fingerprint_uses_image_content(settings, tmp_path, img):
    other = tmp_path / "other.png"
    other.write_bytes(PNG + b"\x01")
    copy = tmp_path / "copy.png"
    copy.write_bytes(PNG)
    fp = build_request(settings, P, image=img).fingerprint()
    assert build_request(settings, P, image=load_image(str(copy), 10_000)).fingerprint() == fp    # 경로 무관
    assert build_request(settings, P, image=load_image(str(other), 10_000)).fingerprint() != fp


def test_text_fingerprint_unchanged(settings):
    """텍스트 요청의 fingerprint 는 이전 형식 그대로다 (업데이트 직후에도 중복 감지가 이어진다)."""
    key = "\n".join([P, "seedance-2.5", "480p", "5", "16:9"])
    assert build_request(settings, P).fingerprint() == hashlib.sha256(key.encode()).hexdigest()[:32]


# ── 설정 검증 ──

BASE = {"alias": "m", "provider": "fal", "model_ref": "a/b", "durations": [5], "price_per_sec": {"480p": 1}}


@pytest.mark.parametrize(
    "inp, extra, msg",
    [
        ({"image_field": "prompt"}, {}, "image_field"),
        ({"image_field": "image_url", "extra": {"image_url": "x"}}, {}, "서버가 채우는"),
        ({"image_field": "image_url", "send_aspect_ratio": False}, {"aspect_ratios": ["16:9"]}, "aspect_ratios"),
        ({"extra": {"aspect_ratio": "auto"}}, {}, "서버가 채우는"),    # 비율을 보내는 모델은 덮어쓸 수 없다
        # 텍스트 모델은 비율을 보내지 않더라도 고정 비율을 extra 로 보낼 수 없다 (기록과 실제가 어긋남)
        ({"send_aspect_ratio": False, "extra": {"aspect_ratio": "16:9"}}, {}, "서버가 채우는"),
    ],
)
def test_invalid_image_model_config(inp, extra, msg):
    with pytest.raises(ValidationError, match=msg):
        ModelConfig.model_validate({**BASE, **extra, "input": inp})


def test_fixed_aspect_ratio_allowed_when_following_image():
    m = ModelConfig.model_validate({**BASE, "input": {"image_field": "image", "send_aspect_ratio": False,
                                                       "extra": {"aspect_ratio": "adaptive"}}})
    assert m.needs_image and m.follows_image_ratio


@pytest.mark.parametrize(
    "change, msg",
    [
        ({"defaults": {"model": "kling-3.0-i2v"}}, "텍스트→영상 모델을 쓴다"),
        ({"defaults": {"image_model": "kling-3.0"}}, "이미지→영상 모델을 쓴다"),
        ({"defaults": {"image_model": "nope"}}, "허용 목록에 없다"),
    ],
)
def test_invalid_image_defaults(settings, change, msg):
    data = settings.model_dump()
    data["defaults"].update(change["defaults"])
    with pytest.raises(ValidationError, match=msg):
        Settings.model_validate(data)


def test_fallback_must_have_same_input_kind(settings):
    data = settings.model_dump()
    for m in data["models"]:
        if m["alias"] == "seedance-2.5-i2v":
            m["fallback_model"] = "seedance-2.5-fal"
    with pytest.raises(ValidationError, match="입력 종류"):
        Settings.model_validate(data)


# ── 서비스 흐름 (mock 제공처) ──

async def test_generate_with_image_uploads_then_creates(make_app, png):
    rep, fal = MockProvider(), MockProvider()
    app = make_app(providers={"replicate": rep, "fal": fal})
    r = await app.generate(P, image_path=str(png))
    assert r["model"] == "seedance-2.5-i2v" and r["image"] == str(png.resolve())
    assert rep.uploads == [hashlib.sha256(PNG).hexdigest()]
    (payload,) = rep.payloads
    assert payload["image"].startswith("https://mock.invalid/inputs/") and payload["aspect_ratio"] == "adaptive"
    job = app.ledger.get(r["job_id"])
    assert job["image"] == str(png.resolve()) and job["aspect_ratio"] == "auto"
    assert (await app.status(r["job_id"], wait_seconds=0))["image"] == str(png.resolve())
    lines = app.audit.path.read_text(encoding="utf-8").splitlines()
    events = {e["event"]: e for e in map(json.loads, lines)}
    assert events["create_requested"]["inputs_sha256"] == [hashlib.sha256(PNG).hexdigest()]
    assert "inputs_uploaded" in events
    assert not any("base64" in line for line in lines)


async def test_same_image_is_duplicate_other_image_is_not(make_app, png, tmp_path):
    mp = MockProvider()
    app = make_app(provider=mp)
    first = await app.generate(P, image_path=str(png))
    again = await app.generate(P, image_path=str(png))
    assert again["duplicate"] is True and again["job_id"] == first["job_id"]
    other = tmp_path / "other.png"
    other.write_bytes(PNG + b"\x02")
    second = await app.generate(P, image_path=str(other))
    assert "duplicate" not in second and len(mp.uploads) == 2


async def test_bad_image_is_rejected_before_reserving(make_app, tmp_path):
    app = make_app()
    with pytest.raises(PolicyError, match="파일이 없습니다"):
        await app.generate(P, image_path=str(tmp_path / "nope.png"))
    assert app.ledger.jobs() == {}


class UploadRejecting(MockProvider):
    async def upload_file(self, media):
        raise ProviderError("replicate: 요청 거부(HTTP 413).")


async def test_upload_failure_fails_over_without_charge(make_app, png):
    fal = MockProvider()
    app = make_app(providers={"replicate": UploadRejecting(), "fal": fal})

    async def yes(message):
        return True
    r = await app.generate(P, image_path=str(png), confirm=yes)     # fal 이 더 비싸서 승인이 필요하다
    assert r["failover_from"] == "seedance-2.5-i2v" and r["model"] == "seedance-2.5-fal-i2v"
    assert "입력 파일 업로드 실패" in r["message"]
    assert fal.payloads[0]["image_url"].startswith("https://mock.invalid/inputs/")


async def test_upload_failure_without_fallback_is_create_failed(make_app, png):
    app = make_app(providers={"replicate": MockProvider(), "fal": UploadRejecting()})
    with pytest.raises(AppError, match="입력 파일 업로드 실패"):
        await app.generate(P, model="kling-3.0-i2v", image_path=str(png))
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED
    assert app.usage()["today_usd"] == 0


class SlowUpload(MockProvider):
    async def upload_file(self, media):
        await asyncio.sleep(3600)


async def test_cancel_during_upload_is_not_charged(make_app, png):
    mp = SlowUpload()
    app = make_app(provider=mp)
    task = asyncio.create_task(app.generate(P, image_path=str(png)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED        # 생성 요청은 나가지 않았다
    assert app.usage()["today_usd"] == 0 and not mp.payloads


async def test_long_upload_keeps_reservation_alive(make_app, png, clock, monkeypatch):
    """업로드가 STALE_RESERVED(5분)보다 길어도 수거기가 예약을 unknown 으로 회수하지 않는다."""
    monkeypatch.setattr(service, "RESERVED_HEARTBEAT_SEC", 0.01)
    holder = {}

    class LongUpload(MockProvider):
        async def upload_file(self, media):
            (job_id,) = holder["app"].ledger.jobs()
            for _ in range(4):                     # 3분씩 4번 = 12분
                clock.advance(minutes=3)
                await asyncio.sleep(0.05)          # 그사이 heartbeat 가 돈다
                await holder["app"].process_job(job_id)
            return await super().upload_file(media)

    app = holder["app"] = make_app(provider=LongUpload())
    r = await app.generate(P, image_path=str(png))
    assert r["status"] == L.QUEUED and app.ledger.get(r["job_id"])["status"] == L.QUEUED
    assert not any(json.loads(x)["event"] == "reserved_expired"
                   for x in app.audit.path.read_text(encoding="utf-8").splitlines())


async def test_reservation_expired_during_upload_is_not_created(make_app, png):
    holder = {}

    class ExpiringUpload(MockProvider):
        async def upload_file(self, media):
            (job_id,) = holder["app"].ledger.jobs()
            holder["app"].ledger.transition(job_id, {L.RESERVED}, status=L.UNKNOWN)   # 다른 프로세스가 회수
            return await super().upload_file(media)

    mp = ExpiringUpload()
    app = holder["app"] = make_app(provider=mp)
    with pytest.raises(AppError, match="예약이 만료돼"):
        await app.generate(P, image_path=str(png))
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and not mp.payloads     # 생성 요청을 보내지 않았다 → 과금 없음
    assert app.usage()["today_usd"] == 0


async def test_error_before_request_is_not_charged(make_app, monkeypatch):
    """생성 요청 전(감사 로그 기록 등)의 오류는 과금 없음으로 기록하고 AppError 로 알린다."""
    mp = MockProvider()
    app = make_app(provider=mp)
    original = app.audit.write

    def failing(event, **kw):
        if event == "create_requested":
            raise OSError("disk full")
        return original(event, **kw)

    monkeypatch.setattr(app.audit, "write", failing)
    with pytest.raises(AppError, match="과금되지 않았습니다"):
        await app.generate(P)
    (job,) = app.ledger.jobs().values()
    assert job["status"] == L.CREATE_FAILED and not mp.payloads


async def test_estimate_compares_same_input_kind(make_app, png):
    app = make_app()
    text = {o["model"] for o in app.estimate()["options"]}
    image = app.estimate(image_path=str(png))
    assert text and all(not m.endswith("-i2v") for m in text)
    assert {o["model"] for o in image["options"]} == {
        "seedance-2.5-i2v", "seedance-2.5-fal-i2v", "kling-3.0-i2v", "wan-2.5-i2v", "veo-3.1-fast-i2v",
        "veo-3.1-lite-i2v", "hailuo-2.3-i2v", "gemini-omni-1.2-flash-i2v"}
    assert all(o["input"] == "image" for o in image["options"])
    assert image["cheapest"] == "gemini-omni-1.2-flash-i2v"      # 360p 5초 $0.1365
    # 비율을 지정하면 이미지 비율을 따르는 모델은 이유와 함께 빠진다
    r = app.estimate(image_path=str(png), aspect_ratio="9:16")
    assert {o["model"] for o in r["options"]} == {"gemini-omni-1.2-flash-i2v"}
    assert any("화면 비율을 따릅니다" in u["reason"] for u in r["unavailable"])


async def test_estimate_reports_duplicate_for_same_image(make_app, png):
    app = make_app()
    r = await app.generate(P, image_path=str(png))
    assert app.estimate(prompt=P, image_path=str(png), model="seedance-2.5-i2v")["duplicate_of"] == r["job_id"]


async def test_generate_with_image_via_protocol(make_app, png):
    server = create_server(make_app())
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "image_path" in tools["generate_video"].input_schema["properties"]
        assert "image_path" in tools["estimate_video"].input_schema["properties"]
        r = await c.call_tool("generate_video", {"prompt": P, "image_path": str(png), "model": "wan-2.5-i2v"})
        assert not r.is_error and r.structured_content["model"] == "wan-2.5-i2v"
        bad = await c.call_tool("generate_video", {"prompt": P, "image_path": str(png), "model": "wan-2.5"})
        assert bad.is_error and "이미지·참조 파일을 받지 않습니다" in bad.content[0].text


# ── 제공처 업로드 ──

class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def test_fal_upload_two_steps(img):
    file_url = "https://v3b.fal.media/files/b/abc/input.png"
    upload_url = "https://storage.example/presigned?sig=1"
    rec = Recorder(httpx.Response(200, json={"upload_url": upload_url, "file_url": file_url}), httpx.Response(200))
    url = await FalProvider(FAKE_FAL, rec.client()).upload_file(img)
    assert url == file_url
    init, put = rec.requests
    assert init.method == "POST"
    assert str(init.url) == "https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3"
    assert init.headers["Authorization"] == f"Key {FAKE_FAL}"
    assert json.loads(init.headers["X-Fal-Object-Lifecycle-Preference"]) == {"expiration_duration_seconds": 86400}
    body = json.loads(init.content)
    assert body["content_type"] == "image/png" and body["file_name"].endswith(".png")
    assert put.method == "PUT" and str(put.url) == upload_url
    assert put.headers["Content-Type"] == "image/png" and put.content == PNG
    assert "Authorization" not in put.headers          # presigned URL 에 키를 보내지 않는다


@pytest.mark.parametrize("status", [307, 403])
async def test_fal_upload_put_failure(img, status):
    rec = Recorder(httpx.Response(200, json={"upload_url": "https://storage.example/p", "file_url": "https://v3b.fal.media/x"}),
                   httpx.Response(status))
    with pytest.raises(ProviderError) as e:
        await FalProvider(FAKE_FAL, rec.client()).upload_file(img)
    assert "입력 파일 전송 실패" in e.value.message and "API 키" not in e.value.message   # 서명 만료를 키 문제로 안내하지 않는다


async def test_fal_upload_bad_response(img):
    rec = Recorder(httpx.Response(200, json={"file_url": "https://v3b.fal.media/x.png"}))
    with pytest.raises(ProviderError, match="upload_url"):
        await FalProvider(FAKE_FAL, rec.client()).upload_file(img)


async def test_replicate_upload_multipart(img):
    get = "https://api.replicate.com/v1/files/file_123"
    rec = Recorder(httpx.Response(201, json={"id": "file_123", "urls": {"get": get}}))
    url = await ReplicateProvider(FAKE_REPLICATE, rec.client()).upload_file(img)
    assert url == get
    (req,) = rec.requests
    assert req.method == "POST" and str(req.url) == "https://api.replicate.com/v1/files"
    assert req.headers["Authorization"] == f"Bearer {FAKE_REPLICATE}"
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="content"' in body and b"Content-Type: image/png" in body and PNG in body


async def test_replicate_upload_rejects_foreign_url(img):
    rec = Recorder(httpx.Response(201, json={"id": "f", "urls": {"get": "https://evil.example/f"}}))
    with pytest.raises(ProviderError, match="urls.get"):
        await ReplicateProvider(FAKE_REPLICATE, rec.client()).upload_file(img)
